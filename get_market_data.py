#!/usr/bin/env python3
"""
Controlled, read-only market-data interface for the OpenClaw data-agent.

This script ONLY imports read-only functions from delta_api (analyze_symbol,
get_current_price, get_candles, indicator computers). It never imports or
calls place_order / buy_market / sell_market / set_stop_loss / set_leverage /
cancel_order / close_position or any other order-mutating function - those
names are not referenced anywhere below, by design, so this script has no
code path that can place, modify, or cancel a trade.

Credential note: delta_api reads DELTA_API_KEY/DELTA_API_SECRET from (in
order) the project .env, the shared ~/.delta-exchange-mcp/config.env, then
explicit environment variables. That shared key currently has Trading
permission (it's the same key the executor uses). For true read-only
credential isolation, create a Delta API key scoped to "Read Data" only and
export it as DELTA_DATA_AGENT_API_KEY / DELTA_DATA_AGENT_API_SECRET before
calling this script - it will prefer those if present (mapped below) and
fall back to the shared key otherwise, printing a one-line warning on stderr
when it does.

Usage:
    get_market_data.py SYMBOL [--timeframes 5m,15m,1h,...] [--period 14]

Output: a single JSON object on stdout, always, success or failure. Never
prints raw exception tracebacks or secrets.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Map a dedicated read-only key (if provided) to the names delta_api reads,
# BEFORE importing delta_api - its credential resolution happens at import
# time and gives explicit os.environ vars top precedence.
if os.environ.get("DELTA_DATA_AGENT_API_KEY") and os.environ.get("DELTA_DATA_AGENT_API_SECRET"):
    os.environ["DELTA_API_KEY"] = os.environ["DELTA_DATA_AGENT_API_KEY"]
    os.environ["DELTA_API_SECRET"] = os.environ["DELTA_DATA_AGENT_API_SECRET"]
else:
    print(
        "get_market_data.py: no DELTA_DATA_AGENT_API_KEY/SECRET set - "
        "falling back to the shared Delta key (has Trading permission). "
        "Create a Read-Data-only key on Delta and export those two vars "
        "for true credential isolation.",
        file=sys.stderr,
    )

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import delta_api  # noqa: E402  (must follow the os.environ setup above)

DEFAULT_TIMEFRAMES = ["5m", "15m", "30m", "1h", "2h", "4h", "6h", "1d"]


def build_market_data(symbol: str, timeframes: list[str], period: int) -> dict:
    result = delta_api.analyze_symbol(symbol, resolutions=timeframes, period=period)

    out_timeframes = {}
    for tf in result["timeframes"]:
        resolution = tf["resolution"]
        if tf.get("error"):
            out_timeframes[resolution] = {"error": tf["error"]}
            continue
        out_timeframes[resolution] = {
            "structure": tf.get("structure"),
            "adx": round(tf["adx"], 2),
            "rsi": round(tf["rsi"], 2),
            "atr": round(tf["atr"], 2),
            "di_plus": round(tf["plus_di"], 2),
            "di_minus": round(tf["minus_di"], 2),
            "volume_ratio": round(tf["volume_ratio"], 4) if tf.get("volume_ratio") is not None else None,
        }

    return {
        "status": "OK",
        "symbol": result["symbol"],
        "price": result["price"],
        "timeframes": out_timeframes,
        "open_interest": {
            "contracts": result.get("oi_contracts"),
            "change_6h": result.get("oi_change_usd_6h"),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Delta market-data snapshot (JSON only)")
    parser.add_argument("symbol")
    parser.add_argument("--timeframes", default=",".join(DEFAULT_TIMEFRAMES), help="comma-separated, e.g. 5m,15m,1h")
    parser.add_argument("--period", type=int, default=14)
    args = parser.parse_args(argv)

    timeframes = [t.strip() for t in args.timeframes.split(",") if t.strip()]

    try:
        data = build_market_data(args.symbol, timeframes, args.period)
    except Exception as exc:  # noqa: BLE001 - this is a CLI boundary, must always emit JSON
        print(json.dumps({"status": "ERROR", "symbol": args.symbol.upper(), "message": str(exc)}))
        return 1

    print(json.dumps(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
