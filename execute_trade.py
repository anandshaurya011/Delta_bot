#!/usr/bin/env python3
"""
Controlled trade-execution interface for the OpenClaw executor agent.

The executor NEVER blindly trusts the coordinator: every trade plan is
independently re-validated here (the "risk guard") before anything touches
delta_api.py. This script is the only thing the executor's exec policy
allowlists - it never runs arbitrary shell commands.

Safety defaults (cannot be bypassed by input JSON):
  - TRADING_MODE env var controls live vs simulated execution. Unset, empty,
    or anything other than the exact string "LIVE" stays DRY_RUN. There is
    no field in the input JSON that can switch this - only the environment.
  - A kill-switch file (KILL_SWITCH, next to this script) blocks ALL
    execution, DRY_RUN included, the instant it exists.
  - MAX_QUANTITY_PER_TRADE / MAX_TOTAL_POSITION / MAX_DAILY_LOSS_USD env
    vars (sane defaults below) are hard caps, not suggestions.

Input: a single JSON object, either as argv[1] or on stdin, e.g.:
    {
      "action": "OPEN_POSITION", "symbol": "BTCUSD", "side": "BUY",
      "quantity": 1, "entry": 86100, "stop_loss": 85700,
      "tp1": 86200, "tp2": 86600, "tp3": 87000, "signal_id": "tv_123"
    }
Supported actions: OPEN_POSITION, CLOSE_POSITION, CANCEL_ORDER.

Output: always exactly one JSON object on stdout. Never a traceback, never
a secret.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import delta_api  # noqa: E402
import trade_db  # noqa: E402

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
KILL_SWITCH_PATH = os.path.join(SCRIPT_DIR, "KILL_SWITCH")

MAX_QUANTITY_PER_TRADE = float(os.environ.get("MAX_QUANTITY_PER_TRADE", "5"))
MAX_TOTAL_POSITION = float(os.environ.get("MAX_TOTAL_POSITION", "5"))
MAX_DAILY_LOSS_USD = float(os.environ.get("MAX_DAILY_LOSS_USD", "50"))

VALID_ACTIONS = {"OPEN_POSITION", "CLOSE_POSITION", "CANCEL_ORDER"}
VALID_SIDES = {"BUY", "SELL"}


def _is_live_mode() -> bool:
    return os.environ.get("TRADING_MODE") == "LIVE"


def validate_open_position(plan: dict) -> list[str]:
    """The risk guard. Returns a list of rejection reasons - empty means approved."""
    reasons: list[str] = []

    symbol = plan.get("symbol")
    side = str(plan.get("side") or "").upper()
    quantity = plan.get("quantity")
    entry = plan.get("entry")
    stop_loss = plan.get("stop_loss")
    tps = [plan.get(k) for k in ("tp1", "tp2", "tp3") if plan.get(k) is not None]

    # 1. valid symbol
    try:
        delta_api.get_product(symbol) if symbol else reasons.append("missing symbol")
    except Exception:
        reasons.append(f"invalid or unknown symbol: {symbol!r}")

    # 2. valid side
    if side not in VALID_SIDES:
        reasons.append(f"side must be BUY or SELL, got {plan.get('side')!r}")

    # 3. quantity > 0
    if not isinstance(quantity, (int, float)) or quantity <= 0:
        reasons.append(f"quantity must be > 0, got {quantity!r}")
        quantity = None

    # 4. quantity <= configured maximum
    if quantity is not None and quantity > MAX_QUANTITY_PER_TRADE:
        reasons.append(f"quantity {quantity} exceeds MAX_QUANTITY_PER_TRADE ({MAX_QUANTITY_PER_TRADE})")

    # 5. entry is reasonable (just: present and positive - sanity, not a market-price check)
    if not isinstance(entry, (int, float)) or entry <= 0:
        reasons.append(f"entry must be a positive number, got {entry!r}")
        entry = None

    # 6. stop loss exists
    if not isinstance(stop_loss, (int, float)):
        reasons.append("stop_loss is required")
        stop_loss = None

    # 7. TP values are valid (at least one, all numeric)
    if not tps:
        reasons.append("at least one of tp1/tp2/tp3 is required")
    elif any(not isinstance(tp, (int, float)) for tp in tps):
        reasons.append("tp1/tp2/tp3 must be numeric where provided")

    # 8/9. side-aware SL/TP ordering
    if side in VALID_SIDES and entry is not None and stop_loss is not None:
        if side == "BUY":
            if not (stop_loss < entry):
                reasons.append(f"BUY requires stop_loss ({stop_loss}) < entry ({entry})")
            for i, tp in enumerate(tps, 1):
                if isinstance(tp, (int, float)) and not (tp > entry):
                    reasons.append(f"BUY requires tp{i} ({tp}) > entry ({entry})")
        else:  # SELL
            if not (stop_loss > entry):
                reasons.append(f"SELL requires stop_loss ({stop_loss}) > entry ({entry})")
            for i, tp in enumerate(tps, 1):
                if isinstance(tp, (int, float)) and not (tp < entry):
                    reasons.append(f"SELL requires tp{i} ({tp}) < entry ({entry})")

    # 10. duplicate signal protection
    signal_id = plan.get("signal_id")
    if signal_id and trade_db.has_executed_signal(signal_id):
        reasons.append(f"signal_id {signal_id!r} was already executed - refusing to open a second position from it")

    # 11. existing-position protection
    if symbol and not reasons:
        try:
            existing = delta_api.get_position(symbol)
            if existing and float(existing.get("size") or 0) != 0:
                reasons.append(f"a position already exists on {symbol} (size={existing.get('size')}) - refusing to stack")
        except Exception as exc:  # noqa: BLE001
            reasons.append(f"could not check existing position: {exc}")

    # 12. maximum total position (after this trade would-be exposure)
    if quantity is not None and quantity > MAX_TOTAL_POSITION:
        reasons.append(f"quantity {quantity} alone exceeds MAX_TOTAL_POSITION ({MAX_TOTAL_POSITION})")

    # 13. maximum daily loss kill-switch
    daily_pnl = trade_db.todays_realized_pnl()
    if daily_pnl <= -abs(MAX_DAILY_LOSS_USD):
        reasons.append(f"daily realized PnL ({daily_pnl:.2f}) has breached -MAX_DAILY_LOSS_USD ({MAX_DAILY_LOSS_USD}) - trading halted for today")

    # 14. emergency kill switch
    if os.path.exists(KILL_SWITCH_PATH):
        reasons.append(f"KILL_SWITCH file present at {KILL_SWITCH_PATH} - all execution is halted")

    return reasons


def execute_open_position(plan: dict) -> dict:
    reasons = validate_open_position(plan)
    trading_mode = "LIVE" if _is_live_mode() else "DRY_RUN"
    signal_id = plan.get("signal_id")

    if reasons:
        trade_db.record_execution(
            signal_id, "OPEN_POSITION", plan.get("symbol"), plan.get("side"), plan.get("quantity"),
            plan.get("entry"), plan.get("stop_loss"), plan.get("tp1"), plan.get("tp2"), plan.get("tp3"),
            trading_mode, "REJECTED", rejection_reasons=reasons,
        )
        return {"status": "REJECTED", "trading_mode": trading_mode, "reasons": reasons}

    if trading_mode == "DRY_RUN":
        simulated = {
            "would_place": "bracket_order",
            "symbol": plan["symbol"], "side": plan["side"].upper(), "quantity": plan["quantity"],
            "entry": plan["entry"], "stop_loss": plan["stop_loss"],
            "take_profits": [plan[k] for k in ("tp1", "tp2", "tp3") if plan.get(k) is not None],
        }
        trade_db.record_execution(
            signal_id, "OPEN_POSITION", plan.get("symbol"), plan.get("side"), plan.get("quantity"),
            plan.get("entry"), plan.get("stop_loss"), plan.get("tp1"), plan.get("tp2"), plan.get("tp3"),
            trading_mode, "VALIDATED_DRY_RUN", order_result=simulated,
        )
        return {"status": "DRY_RUN", "message": "DRY_RUN — NO REAL ORDER SENT", "simulated_order": simulated}

    # LIVE path: only tp1 is wired to the bracket order's take-profit (Delta's
    # bracket endpoint carries a single TP level); tp2/tp3 are logged for the
    # coordinator's own records but are not placed as separate exchange orders
    # - that would need separate reduce-only orders, which is unbuilt scope.
    side = plan["side"].lower()
    order_type = "limit_order" if plan.get("entry") else "market_order"
    try:
        result = delta_api.place_bracket_order(
            plan["symbol"], side=side, size=int(plan["quantity"]), order_type=order_type,
            limit_price=plan.get("entry") if order_type == "limit_order" else None,
            stop_loss_price=plan["stop_loss"], take_profit_price=plan.get("tp1"),
        )
        trade_db.record_execution(
            signal_id, "OPEN_POSITION", plan.get("symbol"), plan.get("side"), plan.get("quantity"),
            plan.get("entry"), plan.get("stop_loss"), plan.get("tp1"), plan.get("tp2"), plan.get("tp3"),
            trading_mode, "EXECUTED", order_result=result,
        )
        return {"status": "EXECUTED", "order_result": result}
    except Exception as exc:  # noqa: BLE001
        trade_db.record_execution(
            signal_id, "OPEN_POSITION", plan.get("symbol"), plan.get("side"), plan.get("quantity"),
            plan.get("entry"), plan.get("stop_loss"), plan.get("tp1"), plan.get("tp2"), plan.get("tp3"),
            trading_mode, "ERROR", rejection_reasons=[str(exc)],
        )
        return {"status": "ERROR", "message": str(exc)}


def execute_close_position(plan: dict) -> dict:
    symbol = plan.get("symbol")
    trading_mode = "LIVE" if _is_live_mode() else "DRY_RUN"
    reasons = []
    if not symbol:
        reasons.append("missing symbol")
    if os.path.exists(KILL_SWITCH_PATH):
        reasons.append(f"KILL_SWITCH file present at {KILL_SWITCH_PATH} - all execution is halted")
    if reasons:
        trade_db.record_execution(plan.get("signal_id"), "CLOSE_POSITION", symbol, None, None, None, None, None, None, None, trading_mode, "REJECTED", rejection_reasons=reasons)
        return {"status": "REJECTED", "trading_mode": trading_mode, "reasons": reasons}

    if trading_mode == "DRY_RUN":
        trade_db.record_execution(plan.get("signal_id"), "CLOSE_POSITION", symbol, None, None, None, None, None, None, None, trading_mode, "VALIDATED_DRY_RUN")
        return {"status": "DRY_RUN", "message": "DRY_RUN — NO REAL ORDER SENT", "simulated_action": f"would close {symbol}"}

    try:
        result = delta_api.close_position(symbol)
        trade_db.record_execution(plan.get("signal_id"), "CLOSE_POSITION", symbol, None, None, None, None, None, None, None, trading_mode, "EXECUTED", order_result=result)
        return {"status": "EXECUTED", "order_result": result}
    except Exception as exc:  # noqa: BLE001
        trade_db.record_execution(plan.get("signal_id"), "CLOSE_POSITION", symbol, None, None, None, None, None, None, None, trading_mode, "ERROR", rejection_reasons=[str(exc)])
        return {"status": "ERROR", "message": str(exc)}


def execute_cancel_order(plan: dict) -> dict:
    symbol = plan.get("symbol")
    order_id = plan.get("order_id")
    trading_mode = "LIVE" if _is_live_mode() else "DRY_RUN"
    reasons = []
    if not symbol:
        reasons.append("missing symbol")
    if not order_id:
        reasons.append("missing order_id")
    if os.path.exists(KILL_SWITCH_PATH):
        reasons.append(f"KILL_SWITCH file present at {KILL_SWITCH_PATH} - all execution is halted")
    if reasons:
        trade_db.record_execution(plan.get("signal_id"), "CANCEL_ORDER", symbol, None, None, None, None, None, None, None, trading_mode, "REJECTED", rejection_reasons=reasons)
        return {"status": "REJECTED", "trading_mode": trading_mode, "reasons": reasons}

    if trading_mode == "DRY_RUN":
        trade_db.record_execution(plan.get("signal_id"), "CANCEL_ORDER", symbol, None, None, None, None, None, None, None, trading_mode, "VALIDATED_DRY_RUN")
        return {"status": "DRY_RUN", "message": "DRY_RUN — NO REAL ORDER SENT", "simulated_action": f"would cancel order {order_id} on {symbol}"}

    try:
        result = delta_api.cancel_order(symbol, int(order_id))
        trade_db.record_execution(plan.get("signal_id"), "CANCEL_ORDER", symbol, None, None, None, None, None, None, None, trading_mode, "EXECUTED", order_result=result)
        return {"status": "EXECUTED", "order_result": result}
    except Exception as exc:  # noqa: BLE001
        trade_db.record_execution(plan.get("signal_id"), "CANCEL_ORDER", symbol, None, None, None, None, None, None, None, trading_mode, "ERROR", rejection_reasons=[str(exc)])
        return {"status": "ERROR", "message": str(exc)}


def main(argv: list[str]) -> int:
    raw = argv[1] if len(argv) > 1 else sys.stdin.read()
    try:
        plan = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(json.dumps({"status": "ERROR", "message": f"invalid JSON input: {exc}"}))
        return 1

    action = plan.get("action")
    if action not in VALID_ACTIONS:
        print(json.dumps({"status": "REJECTED", "reasons": [f"unknown or missing action: {action!r}"]}))
        return 1

    try:
        if action == "OPEN_POSITION":
            result = execute_open_position(plan)
        elif action == "CLOSE_POSITION":
            result = execute_close_position(plan)
        else:
            result = execute_cancel_order(plan)
    except Exception as exc:  # noqa: BLE001 - this is a CLI boundary, must always emit JSON
        print(json.dumps({"status": "ERROR", "message": str(exc)}))
        return 1

    print(json.dumps(result))
    return 0 if result.get("status") != "ERROR" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
