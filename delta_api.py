"""
Delta Exchange India REST API client.

Wraps the account/trading endpoints documented at https://docs.delta.exchange/
for india_prod (api.india.delta.exchange) and india_testnet
(cdn-ind.testnet.deltaex.org). No third-party dependencies: uses only the
standard library.

Credentials, in precedence order (highest wins):
  1. DELTA_API_KEY / DELTA_API_SECRET / DELTA_MCP_ENV already exported in the
     shell environment.
  2. The shared file ~/.delta-exchange-mcp/config.env, written by
     `uvx delta-exchange-mcp login` (this is where this machine's working
     india_prod key currently lives).
  3. The project-local .env next to this file (lowest priority - it still
     holds a testnet key/secret pair that failed login, so don't rely on it
     until it's corrected).

Symbols are things like "BTCUSD" (perpetual futures on Delta India). Most
functions accept a symbol and resolve it to Delta's internal product_id via
GET /v2/products/{symbol}, caching the result.

Quick start (terminal):
    cd /home/anand-shaurya/Documents/personal/delta
    python3
    >>> import delta_api as delta

    # Buy 1 BTCUSD contract at market, with a stop-loss that fires if price
    # drops to 80000 (long position -> stop-loss sells to close it):
    >>> delta.place_bracket_order('BTCUSD', side='buy', size=1, order_type='market_order', stop_loss_price=80000)

    # Sell (short) 1 BTCUSD contract at market, with a stop-loss that fires
    # if price rises to 86000 (short position -> stop-loss buys to close it):
    >>> delta.place_bracket_order('BTCUSD', side='sell', size=1, order_type='market_order', stop_loss_price=86000)

`size` is in contracts, not USD. These place real orders on the live prod
account - check delta.get_current_price('BTCUSD') and size/stop_loss_price
before running one.

Command-line usage (no REPL needed) - run `python3 delta_api.py <command> -h`
for any command's exact options:
    python3 delta_api.py status
    python3 delta_api.py price BTCUSD
    python3 delta_api.py balance
    python3 delta_api.py positions
    python3 delta_api.py max-contracts BTCUSD
    python3 delta_api.py buy BTCUSD 1 --sl 80000 --tp 90000
    python3 delta_api.py sell BTCUSD 1 --sl 86000
    python3 delta_api.py close BTCUSD
    python3 delta_api.py cancel-all BTCUSD
"""

from __future__ import annotations

import argparse
import contextvars
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

BASE_URLS = {
    "india_prod": "https://api.india.delta.exchange",
    "india_testnet": "https://cdn-ind.testnet.deltaex.org",
}

_RELEVANT_KEYS = ("DELTA_API_KEY", "DELTA_API_SECRET", "DELTA_MCP_ENV")
_SHARED_CONFIG_PATH = Path.home() / ".delta-exchange-mcp" / "config.env"
_PROJECT_ENV_PATH = Path(__file__).resolve().parent / ".env"


def _load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _load_config() -> dict[str, str]:
    config: dict[str, str] = {}
    config.update(_load_env_file(_PROJECT_ENV_PATH))
    config.update(_load_env_file(_SHARED_CONFIG_PATH))
    config.update({k: v for k, v in os.environ.items() if k in _RELEVANT_KEYS and v})
    return config


_CONFIG = _load_config()
API_KEY = _CONFIG.get("DELTA_API_KEY")
API_SECRET = _CONFIG.get("DELTA_API_SECRET")
ENV = _CONFIG.get("DELTA_MCP_ENV", "india_prod")
BASE_URL = BASE_URLS.get(ENV, BASE_URLS["india_prod"])

_PRODUCT_CACHE: dict[tuple[str, str], dict[str, Any]] = {}

# Per-request credentials (set by the web backend). None = not in a web request -> module globals (CLI).
_CREDS: contextvars.ContextVar[tuple[str | None, str | None, str] | None] = contextvars.ContextVar("delta_creds", default=None)


class CredentialsMissing(RuntimeError):
    """A signed call was attempted but no Delta credentials are available for this request."""


def use_credentials(api_key: str | None, api_secret: str | None, env: str) -> contextvars.Token:
    """Bind credentials to the current context. Empty keys mean 'none' -- the process globals are NOT consulted."""
    if env not in BASE_URLS:
        raise ValueError(f"unknown Delta environment {env!r}; expected one of {sorted(BASE_URLS)}")
    return _CREDS.set((api_key or None, api_secret or None, env))


def _active() -> tuple[str | None, str | None, str]:
    c = _CREDS.get()
    if c is not None:
        return c[0], c[1], BASE_URLS[c[2]]
    return API_KEY, API_SECRET, BASE_URL


def active_env() -> str:
    c = _CREDS.get()
    return c[2] if c is not None else ENV


class DeltaAPIError(RuntimeError):
    """Raised for any non-2xx response or {"success": false} payload."""

    def __init__(self, status: int, payload: Any):
        self.status = status
        self.payload = payload
        super().__init__(f"HTTP {status}: {payload}")


# --------------------------------------------------------------------------
# Core signed request plumbing
# --------------------------------------------------------------------------

def _sign(method: str, timestamp: str, path: str, query_string: str, body: str, secret: str | None = None) -> str:
    message = method + timestamp + path + query_string + body
    return hmac.new((secret if secret is not None else API_SECRET).encode(), message.encode(), hashlib.sha256).hexdigest()


def _request(
    method: str,
    path: str,
    query: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    auth: bool = True,
) -> Any:
    query = {k: v for k, v in (query or {}).items() if v is not None}
    query_string = ("?" + urllib.parse.urlencode(query)) if query else ""
    payload = json.dumps(body) if body is not None else ""
    api_key, api_secret, base_url = _active()
    url = base_url + path + query_string

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "delta-api-python-client",
    }
    if auth:
        if not api_key or not api_secret:
            raise CredentialsMissing(
                "DELTA_API_KEY / DELTA_API_SECRET not set. Run "
                "`uvx delta-exchange-mcp login`, or export the env vars, or fix .env."
            )
        timestamp = str(int(time.time()))
        headers["api-key"] = api_key
        headers["timestamp"] = timestamp
        headers["signature"] = _sign(method, timestamp, path, query_string, payload, api_secret)

    data = payload.encode() if payload else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            parsed_err = json.loads(raw)
        except json.JSONDecodeError:
            parsed_err = raw
        raise DeltaAPIError(exc.code, parsed_err) from None

    parsed = json.loads(raw) if raw else {}
    if isinstance(parsed, dict) and not parsed.get("success", True):
        raise DeltaAPIError(200, parsed.get("error", parsed))
    return parsed.get("result") if isinstance(parsed, dict) else parsed


# --------------------------------------------------------------------------
# Products / prices
# --------------------------------------------------------------------------

def get_products(contract_types: str | None = None, states: str = "live") -> list[dict]:
    """List products, e.g. contract_types='perpetual_futures'."""
    return _request(
        "GET", "/v2/products", query={"contract_types": contract_types, "states": states}, auth=False
    )


def get_product(symbol: str) -> dict:
    return _request("GET", f"/v2/products/{symbol.upper()}", auth=False)


def get_product_id(symbol: str) -> int:
    symbol = symbol.upper()
    key = (_active()[2], symbol)
    if key not in _PRODUCT_CACHE:
        _PRODUCT_CACHE[key] = get_product(symbol)
    return _PRODUCT_CACHE[key]["id"]


def get_ticker(symbol: str) -> dict:
    symbol = symbol.upper()
    ticker = _request("GET", f"/v2/tickers/{symbol}", auth=False)
    if ticker is None:
        raise DeltaAPIError(200, f"no ticker for symbol '{symbol}' - check the exact product symbol (e.g. 'BTCUSD', not 'BTC')")
    return ticker


def get_current_price(symbol: str) -> float:
    """Best-effort 'current price' - mark price, falling back to last trade."""
    ticker = get_ticker(symbol)
    price = ticker.get("mark_price") or ticker.get("close") or ticker.get("spot_price")
    if price is None:
        quotes = ticker.get("quotes") or {}
        price = quotes.get("best_bid") or quotes.get("best_ask")
    if price is None:
        raise DeltaAPIError(200, f"no price field found in ticker: {ticker}")
    return float(price)


# --------------------------------------------------------------------------
# Candles / technical indicators (ADX: is the market trending or ranging)
# --------------------------------------------------------------------------

_RESOLUTION_SECONDS = {
    "5s": 5, "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "1d": 86400, "1w": 604800,
}


def get_candles(symbol: str, resolution: str = "1h", count: int = 200, end: int | None = None) -> list[dict]:
    """
    OHLC candles (GET /v2/history/candles), oldest first. resolution is one
    of 5s,1m,3m,5m,15m,30m,1h,2h,4h,6h,1d,1w. count is how many candles to
    ask for (best-effort - the exchange may return fewer near listing time).
    """
    seconds = _RESOLUTION_SECONDS.get(resolution)
    if seconds is None:
        raise ValueError(f"resolution must be one of {sorted(_RESOLUTION_SECONDS)}")
    end = end if end is not None else int(time.time())
    start = end - count * seconds
    candles = _request(
        "GET",
        "/v2/history/candles",
        query={"symbol": symbol.upper(), "resolution": resolution, "start": start, "end": end},
        auth=False,
    )
    return sorted(candles, key=lambda c: c["time"])


def compute_dmi(candles: list[dict], period: int = 14) -> dict:
    """
    Wilder's full DMI/ADX system from OHLC candles (oldest first; each
    needs 'high'/'low'/'close'). Returns the most recent {plus_di, minus_di,
    adx}:
      - adx: trend STRENGTH, regardless of direction - below ~20 the
        market is ranging, above ~25 it's trending.
      - plus_di vs minus_di: trend DIRECTION - plus_di > minus_di means the
        trend is up, minus_di > plus_di means down. The size of the gap is
        how strong that direction is.
    Needs at least 2*period+1 candles for a first value; more (get_adx /
    get_trend default to 6*period) lets Wilder's smoothing converge instead
    of still settling from its warm-up.
    """
    if len(candles) < period * 2 + 1:
        raise ValueError(f"need at least {period * 2 + 1} candles, got {len(candles)}")

    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    closes = [c["close"] for c in candles]

    plus_dm, minus_dm, tr = [], [], []
    for i in range(1, len(candles)):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)
        tr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))

    def wilder_smooth(values: list[float]) -> list[float]:
        smoothed = [sum(values[:period])]
        for v in values[period:]:
            smoothed.append(smoothed[-1] - smoothed[-1] / period + v)
        return smoothed

    smoothed_tr = wilder_smooth(tr)
    smoothed_plus_dm = wilder_smooth(plus_dm)
    smoothed_minus_dm = wilder_smooth(minus_dm)

    plus_di_series, minus_di_series, dx = [], [], []
    for tr_i, pdm_i, mdm_i in zip(smoothed_tr, smoothed_plus_dm, smoothed_minus_dm):
        plus_di = 100 * pdm_i / tr_i if tr_i else 0.0
        minus_di = 100 * mdm_i / tr_i if tr_i else 0.0
        plus_di_series.append(plus_di)
        minus_di_series.append(minus_di)
        di_sum = plus_di + minus_di
        dx.append(100 * abs(plus_di - minus_di) / di_sum if di_sum else 0.0)

    if len(dx) < period:
        raise ValueError("not enough candles to compute a stable ADX - fetch more")

    adx = sum(dx[:period]) / period
    for d in dx[period:]:
        adx = (adx * (period - 1) + d) / period

    return {"plus_di": plus_di_series[-1], "minus_di": minus_di_series[-1], "adx": adx}


def compute_adx(candles: list[dict], period: int = 14) -> float:
    """Wilder's ADX only (trend strength, no direction). See compute_dmi for the full system."""
    return compute_dmi(candles, period=period)["adx"]


def get_adx(symbol: str, resolution: str = "1h", period: int = 14) -> float:
    """Fetches enough candles and returns the current ADX value for symbol."""
    candles = get_candles(symbol, resolution=resolution, count=period * 6)
    return compute_adx(candles, period=period)


def is_market_ranging(symbol: str, resolution: str = "1h", period: int = 14, threshold: float = 25) -> dict:
    """
    ADX-based range/trend read - a filter for whether to trust an entry
    signal, not a signal itself. ADX below threshold -> ranging (skip
    signals); at/above threshold -> trending (signals are safe to act on).
    """
    adx = get_adx(symbol, resolution=resolution, period=period)
    return {
        "symbol": symbol.upper(),
        "resolution": resolution,
        "period": period,
        "adx": adx,
        "threshold": threshold,
        "is_ranging": adx < threshold,
    }


def get_trend(symbol: str, resolution: str = "1h", period: int = 14, threshold: float = 25) -> dict:
    """
    Direction + strength in one read, from Wilder's DMI/ADX system:
      - direction: 'up' if +DI > -DI else 'down'.
      - is_trending: True when adx >= threshold (the move has conviction,
        same cutoff as is_market_ranging - a strategy should generally only
        act on direction when is_trending is True).
    """
    candles = get_candles(symbol, resolution=resolution, count=period * 6)
    dmi = compute_dmi(candles, period=period)
    return {
        "symbol": symbol.upper(),
        "resolution": resolution,
        "period": period,
        "plus_di": dmi["plus_di"],
        "minus_di": dmi["minus_di"],
        "adx": dmi["adx"],
        "direction": "up" if dmi["plus_di"] > dmi["minus_di"] else "down",
        "is_trending": dmi["adx"] >= threshold,
        "threshold": threshold,
    }


def compute_rsi(candles: list[dict], period: int = 14) -> float:
    """
    Wilder's RSI (Relative Strength Index) from OHLC candles (oldest first;
    each needs 'close'). Returns the most recent RSI value, 0-100 -
    conventionally >=70 overbought, <=30 oversold. Needs at least period+1
    candles for a first value; more (get_rsi defaults to 6*period) lets
    Wilder's smoothing converge.
    """
    if len(candles) < period + 1:
        raise ValueError(f"need at least {period + 1} candles, got {len(candles)}")

    closes = [c["close"] for c in candles]
    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_atr(candles: list[dict], period: int = 14) -> float:
    """
    Wilder's ATR (Average True Range) from OHLC candles (oldest first;
    each needs 'high'/'low'/'close') - absolute price volatility, in the
    same units as price. Needs at least period+1 candles for a first value;
    more (get_atr defaults to 6*period) lets Wilder's smoothing converge.
    """
    if len(candles) < period + 1:
        raise ValueError(f"need at least {period + 1} candles, got {len(candles)}")

    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    closes = [c["close"] for c in candles]

    tr = []
    for i in range(1, len(candles)):
        tr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))

    atr = sum(tr[:period]) / period
    for t in tr[period:]:
        atr = (atr * (period - 1) + t) / period
    return atr


def get_atr(symbol: str, resolution: str = "1h", period: int = 14) -> float:
    """Fetches enough candles and returns the current ATR value for symbol."""
    candles = get_candles(symbol, resolution=resolution, count=period * 6)
    return compute_atr(candles, period=period)


def compute_market_structure(candles: list[dict], swing_strength: int = 2) -> str | None:
    """
    Classify the most recent confirmed swing point as 'HH' (higher high),
    'LH' (lower high), 'HL' (higher low), or 'LL' (lower low) - classic
    price-action market structure. candles must be oldest-first with
    'high'/'low'. A swing high/low is a candle whose high/low is the
    extreme within swing_strength candles on each side. Returns None if
    there aren't yet two confirmed swings of the relevant kind.
    """
    n = len(candles)
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]

    swing_highs: list[tuple[int, float]] = []
    swing_lows: list[tuple[int, float]] = []
    for i in range(swing_strength, n - swing_strength):
        window = slice(i - swing_strength, i + swing_strength + 1)
        if highs[i] == max(highs[window]):
            swing_highs.append((i, highs[i]))
        if lows[i] == min(lows[window]):
            swing_lows.append((i, lows[i]))

    last_high = swing_highs[-1] if swing_highs else None
    last_low = swing_lows[-1] if swing_lows else None
    if last_high is None and last_low is None:
        return None

    most_recent_is_high = last_high is not None and (last_low is None or last_high[0] > last_low[0])
    if most_recent_is_high:
        if len(swing_highs) < 2:
            return None
        return "HH" if swing_highs[-1][1] > swing_highs[-2][1] else "LH"
    if len(swing_lows) < 2:
        return None
    return "HL" if swing_lows[-1][1] > swing_lows[-2][1] else "LL"


def get_market_structure(symbol: str, resolution: str = "1h", swing_strength: int = 2) -> str | None:
    """Fetches enough candles and returns the current market structure label for symbol."""
    candles = get_candles(symbol, resolution=resolution, count=max(100, swing_strength * 20))
    return compute_market_structure(candles, swing_strength=swing_strength)


def get_rsi(symbol: str, resolution: str = "1h", period: int = 14) -> float:
    """Fetches enough candles and returns the current RSI value for symbol."""
    candles = get_candles(symbol, resolution=resolution, count=period * 6)
    return compute_rsi(candles, period=period)


def get_rsi_reading(
    symbol: str, resolution: str = "1h", period: int = 14, overbought: float = 70, oversold: float = 30
) -> dict:
    """
    RSI value plus a momentum zone: 'overbought' (>= overbought - stretched
    up, pullback/reversal risk), 'oversold' (<= oversold - stretched down),
    else 'neutral'.
    """
    rsi = get_rsi(symbol, resolution=resolution, period=period)
    if rsi >= overbought:
        zone = "overbought"
    elif rsi <= oversold:
        zone = "oversold"
    else:
        zone = "neutral"
    return {
        "symbol": symbol.upper(),
        "resolution": resolution,
        "period": period,
        "rsi": rsi,
        "overbought": overbought,
        "oversold": oversold,
        "zone": zone,
    }


def analyze_symbol(
    symbol: str,
    resolutions: list[str] | None = None,
    period: int = 14,
    adx_threshold: float = 25,
    overbought: float = 70,
    oversold: float = 30,
) -> dict:
    """
    One-shot combined read for symbol: current price, max affordable
    contracts at the account's current leverage for this product (both
    timeframe-independent), plus per-timeframe ADX/trend-direction/RSI.
    resolutions defaults to every supported timeframe (5s..1w) - pass a
    shorter list, e.g. ['15m', '1h', '1d'], to skip the rest.
    """
    resolutions = resolutions or sorted(_RESOLUTION_SECONDS, key=_RESOLUTION_SECONDS.get)
    ticker = get_ticker(symbol)
    price = ticker.get("mark_price") or ticker.get("close") or ticker.get("spot_price")
    price = float(price) if price is not None else get_current_price(symbol)
    volume = ticker.get("volume")
    oi_contracts = ticker.get("oi_contracts")
    oi_value_usd = ticker.get("oi_value_usd")
    oi_change_usd_6h = ticker.get("oi_change_usd_6h")

    leverage = get_leverage(symbol)
    product = get_product(symbol)
    contract_value = float(product["contract_value"])
    available_balance = get_available_balance("USD")
    margin_per_contract = (contract_value * price) / leverage if leverage else 0
    max_contracts = int(available_balance // margin_per_contract) if margin_per_contract else 0

    timeframes = []
    for resolution in resolutions:
        try:
            candles = get_candles(symbol, resolution=resolution, count=period * 6)
            dmi = compute_dmi(candles, period=period)
            rsi = compute_rsi(candles, period=period)
            atr = compute_atr(candles, period=period)
            structure = compute_market_structure(candles)
            rsi_zone = "overbought" if rsi >= overbought else "oversold" if rsi <= oversold else "neutral"

            candle_volumes = [c["volume"] for c in candles]
            current_volume = candle_volumes[-1]
            baseline_volumes = candle_volumes[-period - 1 : -1]
            volume_avg = sum(baseline_volumes) / len(baseline_volumes) if baseline_volumes else None
            volume_ratio = (current_volume / volume_avg) if volume_avg else None

            timeframes.append(
                {
                    "resolution": resolution,
                    "adx": dmi["adx"],
                    "plus_di": dmi["plus_di"],
                    "minus_di": dmi["minus_di"],
                    "direction": "up" if dmi["plus_di"] > dmi["minus_di"] else "down",
                    "is_trending": dmi["adx"] >= adx_threshold,
                    "rsi": rsi,
                    "rsi_zone": rsi_zone,
                    "atr": atr,
                    "structure": structure,
                    "volume": current_volume,
                    "volume_avg": volume_avg,
                    "volume_ratio": volume_ratio,
                    "error": None,
                }
            )
        except (DeltaAPIError, ValueError) as exc:
            timeframes.append({"resolution": resolution, "error": str(exc)})

    return {
        "symbol": symbol.upper(),
        "price": price,
        "volume": float(volume) if volume is not None else None,
        "oi_contracts": float(oi_contracts) if oi_contracts is not None else None,
        "oi_value_usd": float(oi_value_usd) if oi_value_usd is not None else None,
        "oi_change_usd_6h": float(oi_change_usd_6h) if oi_change_usd_6h is not None else None,
        "leverage": leverage,
        "max_contracts": max_contracts,
        "timeframes": timeframes,
    }


# --------------------------------------------------------------------------
# Account: balances / positions
# --------------------------------------------------------------------------

def get_balances(asset_symbol: str | None = None) -> list[dict]:
    result = _request("GET", "/v2/wallet/balances", auth=True)
    if asset_symbol:
        return [b for b in result if b.get("asset_symbol", "").upper() == asset_symbol.upper()]
    return result


def get_available_balance(asset_symbol: str = "USD") -> float:
    balances = get_balances(asset_symbol)
    return float(balances[0]["available_balance"]) if balances else 0.0


def get_positions(symbol: str | None = None) -> Any:
    product_id = get_product_id(symbol) if symbol else None
    return _request("GET", "/v2/positions", query={"product_id": product_id}, auth=True)


def get_position(symbol: str) -> dict | None:
    result = get_positions(symbol)
    if isinstance(result, list):
        return result[0] if result else None
    return result or None


def get_margined_positions(symbol: str | None = None) -> Any:
    """
    Like get_positions(), but from GET /v2/positions/margined, which includes
    the mark-to-market fields (mark_price, unrealized_pnl, margin, ...) that
    plain GET /v2/positions omits.
    """
    product_id = get_product_id(symbol) if symbol else None
    return _request("GET", "/v2/positions/margined", query={"product_id": product_id}, auth=True)


def get_positions_pnl(symbol: str | None = None) -> list[dict]:
    """
    Open positions with unrealized (and realized, where present) PnL.

    Reads GET /v2/positions/margined. Delta's own unrealized_pnl field is
    used when present; if it's missing it's derived as
    (mark_price - entry_price) * size * contract_value, where size is signed
    (positive = long, negative = short). Note: Delta's upstream unrealized_pnl
    is known to be unsigned/buggy specifically for short options positions -
    not a concern for perpetual futures like BTCUSD.
    """
    result = get_margined_positions(symbol)
    positions = result if isinstance(result, list) else ([result] if result else [])
    pnl_list = []
    for pos in positions:
        size = float(pos.get("size") or 0)
        if size == 0:
            continue
        entry_price = pos.get("entry_price")
        mark_price = pos.get("mark_price")
        contract_value = float(pos.get("contract_value") or 1)
        unrealized_pnl = pos.get("unrealized_pnl")
        if unrealized_pnl is None and entry_price is not None and mark_price is not None:
            unrealized_pnl = (float(mark_price) - float(entry_price)) * size * contract_value
        realized_pnl = pos.get("realized_pnl")
        pnl_list.append(
            {
                "symbol": pos.get("product_symbol"),
                "side": "long" if size > 0 else "short",
                "size": size,
                "entry_price": float(entry_price) if entry_price is not None else None,
                "mark_price": float(mark_price) if mark_price is not None else None,
                "unrealized_pnl": float(unrealized_pnl) if unrealized_pnl is not None else None,
                "realized_pnl": float(realized_pnl) if realized_pnl is not None else None,
            }
        )
    return pnl_list


def get_total_unrealized_pnl() -> float:
    """Sum of unrealized PnL across every open position."""
    return sum(p["unrealized_pnl"] for p in get_positions_pnl() if p["unrealized_pnl"] is not None)


# --------------------------------------------------------------------------
# Orders: place / list / cancel
# --------------------------------------------------------------------------

def get_open_orders(symbol: str | None = None, states: str = "open,pending") -> list[dict]:
    query: dict[str, Any] = {"states": states}
    if symbol:
        query["product_ids"] = str(get_product_id(symbol))
    return _request("GET", "/v2/orders", query=query, auth=True)


def place_order(
    symbol: str,
    side: str,
    size: int,
    order_type: str = "market_order",
    limit_price: str | float | None = None,
    time_in_force: str = "gtc",
    post_only: bool = False,
    reduce_only: bool = False,
    client_order_id: str | None = None,
) -> dict:
    """side: 'buy' or 'sell'. order_type: 'market_order' or 'limit_order'."""
    body: dict[str, Any] = {
        "product_id": get_product_id(symbol),
        "side": side,
        "size": size,
        "order_type": order_type,
        "time_in_force": time_in_force,
        "post_only": post_only,
        "reduce_only": reduce_only,
    }
    if order_type == "limit_order":
        if limit_price is None:
            raise ValueError("limit_price is required for order_type='limit_order'")
        body["limit_price"] = str(limit_price)
    if client_order_id:
        body["client_order_id"] = client_order_id
    return _request("POST", "/v2/orders", body=body, auth=True)


def buy_market(symbol: str, size: int, **kwargs: Any) -> dict:
    return place_order(symbol, "buy", size, order_type="market_order", **kwargs)


def sell_market(symbol: str, size: int, **kwargs: Any) -> dict:
    return place_order(symbol, "sell", size, order_type="market_order", **kwargs)


def buy_limit(symbol: str, size: int, price: str | float, **kwargs: Any) -> dict:
    """'Buy at' a specific price - resting limit order."""
    return place_order(symbol, "buy", size, order_type="limit_order", limit_price=price, **kwargs)


def sell_limit(symbol: str, size: int, price: str | float, **kwargs: Any) -> dict:
    """'Sell at' a specific price - resting limit order."""
    return place_order(symbol, "sell", size, order_type="limit_order", limit_price=price, **kwargs)


def cancel_order(symbol: str, order_id: int) -> dict:
    body = {"id": order_id, "product_id": get_product_id(symbol)}
    return _request("DELETE", "/v2/orders", body=body, auth=True)


def cancel_all_orders(symbol: str | None = None) -> dict:
    body = {"product_id": get_product_id(symbol)} if symbol else {}
    return _request("DELETE", "/v2/orders/all", body=body, auth=True)


# --------------------------------------------------------------------------
# Stand-alone stop-loss / take-profit orders (attached to an existing position)
# --------------------------------------------------------------------------

def place_stop_order(
    symbol: str,
    side: str,
    size: int,
    stop_price: str | float | None = None,
    order_type: str = "market_order",
    limit_price: str | float | None = None,
    stop_order_type: str = "stop_loss_order",
    stop_trigger_method: str = "last_traded_price",
    trail_amount: str | float | None = None,
    reduce_only: bool = True,
) -> dict:
    """
    side is the CLOSING side (e.g. 'sell' to stop out a long position).
    stop_order_type: 'stop_loss_order' or 'take_profit_order'.
    Exactly one of stop_price (fixed trigger) or trail_amount (trailing
    trigger, re-armed as price moves in your favor) must be given.
    """
    if stop_price is None and trail_amount is None:
        raise ValueError("pass stop_price (fixed) or trail_amount (trailing)")
    body: dict[str, Any] = {
        "product_id": get_product_id(symbol),
        "side": side,
        "size": size,
        "order_type": order_type,
        "stop_order_type": stop_order_type,
        "stop_trigger_method": stop_trigger_method,
        "reduce_only": reduce_only,
    }
    if stop_price is not None:
        body["stop_price"] = str(stop_price)
    if trail_amount is not None:
        body["trail_amount"] = str(trail_amount)
    if order_type == "limit_order":
        if limit_price is None:
            raise ValueError("limit_price is required when order_type='limit_order'")
        body["limit_price"] = str(limit_price)
    return _request("POST", "/v2/orders", body=body, auth=True)


def set_stop_loss(symbol: str, side: str, size: int, stop_price: str | float, **kwargs: Any) -> dict:
    """Attach a stop-loss to a position. side = closing side ('sell' for a long)."""
    return place_stop_order(symbol, side, size, stop_price, stop_order_type="stop_loss_order", **kwargs)


def set_take_profit(symbol: str, side: str, size: int, stop_price: str | float, **kwargs: Any) -> dict:
    """Attach a take-profit to a position. side = closing side ('sell' for a long)."""
    return place_stop_order(symbol, side, size, stop_price, stop_order_type="take_profit_order", **kwargs)


def set_trailing_stop_loss(
    symbol: str, side: str, size: int, trail_amount: str | float, **kwargs: Any
) -> dict:
    """
    Attach a trailing stop-loss to a position. side = closing side ('sell'
    for a long, 'buy' for a short). trail_amount is a price distance (not a
    percent), e.g. 500 means the stop stays 500 USD behind the best price
    seen since the order was placed.
    """
    return place_stop_order(
        symbol, side, size, stop_price=None, trail_amount=trail_amount, stop_order_type="stop_loss_order", **kwargs
    )


def get_trailing_stop_orders(symbol: str) -> list[dict]:
    """Open orders on this symbol that are trailing stops (have a trail_amount set)."""
    orders = get_open_orders(symbol)
    return [o for o in orders if o.get("trail_amount") not in (None, "0", 0, "0.0")]


def cancel_trailing_stop(symbol: str) -> list[dict]:
    """
    Cancel every open trailing stop order on this symbol (orders placed via
    set_trailing_stop_loss). Returns the list of cancel results - empty if
    there wasn't one open.
    """
    return [cancel_order(symbol, order["id"]) for order in get_trailing_stop_orders(symbol)]


# --------------------------------------------------------------------------
# Bracket orders (position-level SL + TP together, POST/PUT /v2/orders/bracket)
# --------------------------------------------------------------------------

def place_bracket_order(
    symbol: str,
    side: str,
    size: int,
    order_type: str = "market_order",
    limit_price: str | float | None = None,
    stop_loss_price: str | float | None = None,
    stop_loss_limit_price: str | float | None = None,
    take_profit_price: str | float | None = None,
    take_profit_limit_price: str | float | None = None,
    trail_amount: str | float | None = None,
    bracket_stop_trigger_method: str = "last_traded_price",
) -> dict:
    """Opens a position (or adds to one) with SL and/or TP attached in one call."""
    body: dict[str, Any] = {
        "product_id": get_product_id(symbol),
        "side": side,
        "size": size,
        "order_type": order_type,
        "bracket_stop_trigger_method": bracket_stop_trigger_method,
    }
    if order_type == "limit_order":
        if limit_price is None:
            raise ValueError("limit_price is required when order_type='limit_order'")
        body["limit_price"] = str(limit_price)

    if stop_loss_price is not None:
        stop_loss_order: dict[str, Any] = {"stop_price": str(stop_loss_price)}
        if stop_loss_limit_price is not None:
            stop_loss_order["limit_price"] = str(stop_loss_limit_price)
        if trail_amount is not None:
            stop_loss_order["trail_amount"] = str(trail_amount)
        body["stop_loss_order"] = stop_loss_order

    if take_profit_price is not None:
        take_profit_order: dict[str, Any] = {"stop_price": str(take_profit_price)}
        if take_profit_limit_price is not None:
            take_profit_order["limit_price"] = str(take_profit_limit_price)
        body["take_profit_order"] = take_profit_order

    return _request("POST", "/v2/orders/bracket", body=body, auth=True)


def edit_bracket_order(
    symbol: str,
    stop_loss_price: str | float | None = None,
    stop_loss_limit_price: str | float | None = None,
    take_profit_price: str | float | None = None,
    take_profit_limit_price: str | float | None = None,
    trail_amount: str | float | None = None,
) -> dict:
    """Edit the SL/TP levels of the existing bracket order on this product."""
    body: dict[str, Any] = {"product_id": get_product_id(symbol)}
    if stop_loss_price is not None:
        body["bracket_stop_loss_price"] = str(stop_loss_price)
    if stop_loss_limit_price is not None:
        body["bracket_stop_loss_limit_price"] = str(stop_loss_limit_price)
    if take_profit_price is not None:
        body["bracket_take_profit_price"] = str(take_profit_price)
    if take_profit_limit_price is not None:
        body["bracket_take_profit_limit_price"] = str(take_profit_limit_price)
    if trail_amount is not None:
        body["bracket_trail_amount"] = str(trail_amount)
    return _request("PUT", "/v2/orders/bracket", body=body, auth=True)


# --------------------------------------------------------------------------
# Leverage / position management
# --------------------------------------------------------------------------

def get_leverage(symbol: str) -> float:
    product_id = get_product_id(symbol)
    result = _request("GET", f"/v2/products/{product_id}/orders/leverage", auth=True)
    return float(result["leverage"])


def set_leverage(symbol: str, leverage: str | float) -> dict:
    product_id = get_product_id(symbol)
    return _request(
        "POST", f"/v2/products/{product_id}/orders/leverage", body={"leverage": str(leverage)}, auth=True
    )


def close_position(symbol: str) -> dict | None:
    """Flatten the open position on this product with a reduce-only market order."""
    position = get_position(symbol)
    if not position:
        return None
    size = int(float(position.get("size", 0)))
    if size == 0:
        return None
    side = "sell" if size > 0 else "buy"
    return place_order(symbol, side, abs(size), order_type="market_order", reduce_only=True)


def get_max_contracts(
    symbol: str,
    leverages: list[str | float] | None = None,
    price: str | float | None = None,
    asset_symbol: str = "USD",
) -> list[dict]:
    """
    Best-effort estimate of the exact 'size' (number of contracts) you can
    buy or sell right now on this perpetual, one entry per leverage level:

        max_contracts = floor(available_balance * leverage / (contract_value * price))

    leverages defaults to a standard ladder (1x up to this product's max
    leverage, from its 'default_leverage' field) plus whatever leverage is
    currently set on your account for this product. price defaults to the
    current mark price. This ignores taker fees and Delta's tiered margin
    brackets that raise the requirement at larger sizes (see
    get_product(symbol)['position_size_limit'] for the hard cap) - treat
    max_contracts as an upper bound, not a guarantee the exchange will
    accept it.
    """
    product = get_product(symbol)
    contract_value = float(product["contract_value"])
    current_leverage = get_leverage(symbol)

    if leverages is None:
        max_leverage = float(product.get("default_leverage") or current_leverage)
        ladder = [1, 2, 3, 5, 10, 20, 25, 50, 75, 100, 150, 200]
        leverages = sorted({lv for lv in ladder if lv <= max_leverage} | {current_leverage})
    else:
        leverages = sorted(float(lv) for lv in leverages)

    price = float(price) if price is not None else get_current_price(symbol)
    available_balance = get_available_balance(asset_symbol)

    results = []
    for leverage in leverages:
        margin_per_contract = (contract_value * price) / leverage
        max_contracts = int(available_balance // margin_per_contract) if margin_per_contract > 0 else 0
        results.append(
            {
                "leverage": leverage,
                "is_current_account_leverage": leverage == current_leverage,
                "margin_per_contract": margin_per_contract,
                "max_contracts": max_contracts,
            }
        )
    return results


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _print_result(label: str, result: Any) -> None:
    print(f"{label}:")
    print(json.dumps(result, indent=2, default=str))


_ANSI = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "cyan": "\033[36m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
}


def _color(text: str, *styles: str) -> str:
    """No-op (plain text) when stdout isn't a real terminal, e.g. piped/redirected output."""
    if not sys.stdout.isatty():
        return text
    return "".join(_ANSI[s] for s in styles) + text + _ANSI["reset"]


def _print_table(
    title: str,
    columns: list[str],
    rows: list[tuple[list[str], list[tuple] | None]],
    subtitle: str | None = None,
    footer: str | None = None,
) -> None:
    """rows: list of (cell strings, per-cell ANSI style tuples or None/() for plain)."""
    widths = [len(c) for c in columns]
    for cells, _ in rows:
        for i, cell in enumerate(cells):
            widths[i] = max(widths[i], len(cell))

    def border(left: str, mid: str, right: str) -> str:
        return left + mid.join("─" * (w + 2) for w in widths) + right

    def row_line(cells: list[str], styles: list[tuple] | None = None) -> str:
        parts = []
        for i, cell in enumerate(cells):
            text = cell.rjust(widths[i])
            if styles and styles[i]:
                text = _color(text, *styles[i])
            parts.append(f" {text} ")
        return "│" + "│".join(parts) + "│"

    heading = _color(f" {title} ", "bold", "cyan")
    if subtitle:
        heading += _color(f" {subtitle}", "dim")
    print(heading)
    print(border("┌", "┬", "┐"))
    print(row_line(columns, styles=[("bold",)] * len(columns)))
    print(border("├", "┼", "┤"))
    for cells, styles in rows:
        print(row_line(cells, styles))
    print(border("└", "┴", "┘"))
    if footer:
        print(_color(footer, "dim"))


def _print_balances_summary(balances: list[dict]) -> None:
    nonzero = [b for b in balances if float(b.get("balance") or 0) != 0]
    if not nonzero:
        print("No balance.")
        return
    rows = [
        (
            [b["asset_symbol"], b["available_balance"], f"₹{float(b['available_balance_inr']):,.2f}"],
            [("green",), ("green",), ("green",)],
        )
        for b in nonzero
    ]
    _print_table("Balances", ["Asset", "Available", "Available (INR)"], rows)


def _print_positions_table(positions: list[dict]) -> None:
    if not positions:
        print("No open positions.")
        return
    rows = []
    for p in positions:
        side_style = ("green",) if p["side"] == "long" else ("red",)
        pnl = p.get("unrealized_pnl")
        pnl_str = f"{pnl:+,.4f}" if pnl is not None else "n/a"
        pnl_style = ("green",) if (pnl or 0) > 0 else ("red",) if (pnl or 0) < 0 else ("dim",)
        realized = p.get("realized_pnl")
        realized_str = f"{realized:+,.4f}" if realized is not None else "n/a"
        cells = [
            p["symbol"] or "?",
            p["side"].upper(),
            f"{p['size']:g}",
            f"{p['entry_price']:,.2f}" if p["entry_price"] is not None else "n/a",
            f"{p['mark_price']:,.2f}" if p["mark_price"] is not None else "n/a",
            pnl_str,
            realized_str,
        ]
        styles = [(), side_style, (), (), (), pnl_style, ()]
        rows.append((cells, styles))
    _print_table(
        "Open Positions",
        ["Symbol", "Side", "Size", "Entry", "Mark", "Unrealized PnL", "Realized PnL"],
        rows,
    )


def _print_max_contracts_table(symbol: str, rows_in: list[dict]) -> None:
    usd_balances = get_balances("USD")
    inr_rate = None
    if usd_balances and float(usd_balances[0]["available_balance"]) > 0:
        inr_rate = float(usd_balances[0]["available_balance_inr"]) / float(usd_balances[0]["available_balance"])

    rows = []
    for row in rows_in:
        leverage_label = f"{row['leverage']:g}x" + (" *" if row["is_current_account_leverage"] else "")
        margin_usd = row["margin_per_contract"]
        margin_inr_str = f"{margin_usd * inr_rate:,.2f}" if inr_rate else "n/a"
        cells = [leverage_label, f"{margin_usd:,.4f}", margin_inr_str, str(row["max_contracts"])]
        if row["is_current_account_leverage"]:
            styles = [("yellow", "bold")] * 4
        elif row["max_contracts"] == 0:
            styles = [("dim",)] * 4
        else:
            styles = [("green",)] * 4
        rows.append((cells, styles))

    _print_table(
        f"{symbol.upper()} max contracts by leverage",
        ["Leverage", "Margin/Contract (USD)", "Margin/Contract (INR)", "Max Contracts"],
        rows,
        subtitle="(margin in USD)",
        footer="* = your account's current leverage for this product",
    )


_ORDER_COLUMNS = ["ID", "Symbol", "Side", "Type", "Size", "Filled", "Avg Price", "Stop/Trail", "State", "Commission"]


def _order_row(order: dict) -> tuple[list[str], list[tuple]]:
    size = order.get("size") or 0
    unfilled = order.get("unfilled_size")
    filled = size - unfilled if unfilled is not None else "-"
    avg_price = order.get("average_fill_price")
    stop_price = order.get("stop_price")
    trail_amount = order.get("trail_amount")
    bracket_sl = order.get("bracket_stop_loss_price")
    bracket_tp = order.get("bracket_take_profit_price")

    if bracket_sl or bracket_tp:
        parts = []
        if bracket_sl:
            parts.append(f"SL {bracket_sl}")
        if bracket_tp:
            parts.append(f"TP {bracket_tp}")
        trigger = " / ".join(parts)
    elif trail_amount:
        trigger = f"trail {trail_amount}"
    elif stop_price:
        trigger = f"stop {stop_price}"
    else:
        trigger = "-"

    side = (order.get("side") or "?").upper()
    state = order.get("state") or "?"
    commission = order.get("paid_commission")

    cells = [
        str(order.get("id", "-")),
        order.get("product_symbol", "?"),
        side,
        order.get("order_type", "?"),
        str(size),
        str(filled),
        f"{float(avg_price):,.2f}" if avg_price not in (None, "0") else "-",
        trigger,
        state,
        f"{float(commission):,.4f}" if commission is not None else "-",
    ]
    side_style = ("green",) if side == "BUY" else ("red",) if side == "SELL" else ()
    state_style = ("green",) if state in ("closed", "filled") else ("yellow",) if state in ("open", "pending") else ("dim",)
    styles = [(), (), side_style, (), (), (), (), (), state_style, ()]
    return cells, styles


def _print_order_table(order: dict, title: str = "Order") -> None:
    _print_table(title, _ORDER_COLUMNS, [_order_row(order)])


def _print_orders_table(orders: list[dict], title: str = "Open Orders") -> None:
    if not orders:
        print("No open orders.")
        return
    _print_table(title, _ORDER_COLUMNS, [_order_row(o) for o in orders])


def _print_analysis_table(result: dict) -> None:
    volume = f"{result['volume']:,.2f}" if result.get("volume") is not None else "n/a"
    oi_contracts = f"{result['oi_contracts']:,.0f}" if result.get("oi_contracts") is not None else "n/a"
    oi_usd = f"${result['oi_value_usd']:,.0f}" if result.get("oi_value_usd") is not None else "n/a"
    oi_change = result.get("oi_change_usd_6h")
    oi_change_str = f"{oi_change:+,.0f}" if oi_change is not None else "n/a"
    oi_change_style = ("green",) if (oi_change or 0) > 0 else ("red",) if (oi_change or 0) < 0 else ("dim",)

    print(
        f"{result['symbol']}  price={result['price']:,.2f} USD  "
        f"leverage={result['leverage']:g}x  max_contracts={result['max_contracts']}"
    )
    print(
        f"  24h volume={volume}  open interest={oi_contracts} contracts ({oi_usd})  "
        f"OI change (6h)={_color(oi_change_str, *oi_change_style)}"
    )

    columns = ["Timeframe", "ADX", "RSI", "ATR", "Zone", "Strength", "Direction", "Structure", "DI+", "DI-", "Volume", "Vol Avg", "Vol Ratio"]
    rows = []
    for tf in result["timeframes"]:
        if tf.get("error"):
            cells = [tf["resolution"], "error", tf["error"][:40]] + ["-"] * (len(columns) - 3)
            rows.append((cells, [("dim",)] * len(columns)))
            continue
        strength_label = "trending" if tf["is_trending"] else "ranging"
        strength_style = ("green",) if tf["is_trending"] else ("yellow",)
        direction_style = ("green",) if tf["direction"] == "up" else ("red",)
        zone = tf["rsi_zone"]
        zone_style = ("red",) if zone == "overbought" else ("green",) if zone == "oversold" else ("dim",)
        volume_ratio = tf.get("volume_ratio")
        volume_ratio_str = f"{volume_ratio:.2f}x" if volume_ratio is not None else "n/a"
        volume_ratio_style = ("green",) if (volume_ratio or 0) >= 1.5 else ("dim",) if volume_ratio is None else ()
        if tf["plus_di"] >= tf["minus_di"]:
            plus_di_style, minus_di_style = ("green",), ("dim",)
        else:
            plus_di_style, minus_di_style = ("dim",), ("red",)
        cells = [
            tf["resolution"],
            f"{tf['adx']:.2f}",
            f"{tf['rsi']:.1f}",
            f"{tf['atr']:,.2f}",
            zone,
            strength_label,
            tf["direction"].upper(),
            tf.get("structure") or "n/a",
            f"{tf['plus_di']:.2f}",
            f"{tf['minus_di']:.2f}",
            f"{tf['volume']:,.2f}",
            f"{tf['volume_avg']:,.2f}" if tf.get("volume_avg") is not None else "n/a",
            volume_ratio_str,
        ]
        structure_style = ("green",) if tf.get("structure") in ("HH", "HL") else ("red",) if tf.get("structure") in ("LH", "LL") else ("dim",)
        styles = [
            (), (), (), (), zone_style, strength_style, direction_style, structure_style,
            plus_di_style, minus_di_style, (), (), volume_ratio_style,
        ]
        rows.append((cells, styles))
    _print_table(f"{result['symbol']} analysis", columns, rows)


def _symbols_in_play() -> list[str]:
    """Symbols with an open position or open order right now, for the status summary."""
    symbols = {p["symbol"] for p in get_positions_pnl() if p.get("symbol")}
    orders = get_open_orders()
    if orders:
        id_to_symbol = {product["id"]: product["symbol"] for product in get_products(states="live")}
        for order in orders:
            symbol = id_to_symbol.get(order.get("product_id"))
            if symbol:
                symbols.add(symbol)
    return sorted(symbols) or ["BTCUSD"]


def _sub(sub, name: str, help: str, examples: list[str]) -> argparse.ArgumentParser:
    """add_parser with an examples block shown by `delta_api.py <name> -h`."""
    epilog = "examples:\n" + "\n".join(f"  {e}" for e in examples)
    return sub.add_parser(name, help=help, epilog=epilog, formatter_class=argparse.RawDescriptionHelpFormatter)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="delta_api.py", description="Delta Exchange India CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    _sub(sub, "status", "env/credentials/balances/open-orders sanity check", [
        "python3 delta_api.py status",
    ])

    p = _sub(sub, "price", "current mark price", [
        "python3 delta_api.py price BTCUSD",
    ])
    p.add_argument("symbol")

    p = _sub(sub, "adx", "ADX reading - is the market trending or ranging", [
        "python3 delta_api.py adx BTCUSD",
        "python3 delta_api.py adx BTCUSD -t 15m",
        "python3 delta_api.py adx BTCUSD -t 1d --period 20 --threshold 30",
    ])
    p.add_argument("symbol")
    p.add_argument("-t", "--time", "--resolution", dest="resolution", default="1h", choices=sorted(_RESOLUTION_SECONDS))
    p.add_argument("--period", type=int, default=14)
    p.add_argument("--threshold", type=float, default=25, help="ADX below this = ranging (default 25)")

    p = _sub(sub, "trend", "trend direction (up/down) + strength (DMI/ADX)", [
        "python3 delta_api.py trend BTCUSD",
        "python3 delta_api.py trend BTCUSD -t 15m",
        "python3 delta_api.py trend BTCUSD -t 1d --threshold 20",
    ])
    p.add_argument("symbol")
    p.add_argument("-t", "--time", "--resolution", dest="resolution", default="1h", choices=sorted(_RESOLUTION_SECONDS))
    p.add_argument("--period", type=int, default=14)
    p.add_argument("--threshold", type=float, default=25, help="ADX below this = not trending (default 25)")

    p = _sub(sub, "rsi", "RSI reading - overbought/oversold momentum", [
        "python3 delta_api.py rsi BTCUSD",
        "python3 delta_api.py rsi BTCUSD -t 15m",
        "python3 delta_api.py rsi BTCUSD -t 1d --overbought 75 --oversold 25",
    ])
    p.add_argument("symbol")
    p.add_argument("-t", "--time", "--resolution", dest="resolution", default="1h", choices=sorted(_RESOLUTION_SECONDS))
    p.add_argument("--period", type=int, default=14)
    p.add_argument("--overbought", type=float, default=70)
    p.add_argument("--oversold", type=float, default=30)

    p = _sub(sub, "analysis", "combined ADX/trend/RSI/sizing/price table for a symbol", [
        "python3 delta_api.py analysis BTCUSD",
        "python3 delta_api.py analysis BTCUSD -t 15m",
        "python3 delta_api.py analysis BTCUSD -t all",
        "python3 delta_api.py analysis BTCUSD -t all --raw",
    ])
    p.add_argument("symbol")
    p.add_argument(
        "-t", "--time", "--resolution", dest="resolution", default="1h",
        help="timeframe (e.g. 1h), or 'all' for every supported timeframe",
    )
    p.add_argument("--period", type=int, default=14)
    p.add_argument("--threshold", type=float, default=25, help="ADX trending threshold (default 25)")
    p.add_argument("--overbought", type=float, default=70)
    p.add_argument("--oversold", type=float, default=30)
    p.add_argument("--raw", action="store_true", help="print raw JSON instead of the table")

    p = _sub(sub, "balance", "available balance (USD and INR) for each non-zero asset", [
        "python3 delta_api.py balance",
        "python3 delta_api.py balance usd",
        "python3 delta_api.py balance usd --raw",
    ])
    p.add_argument("asset", nargs="?", default=None, help="filter to one asset, e.g. USD (case-insensitive)")
    p.add_argument("--raw", action="store_true", help="print the full raw balance objects instead of the summary")

    p = _sub(sub, "positions", "open positions with unrealized PnL", [
        "python3 delta_api.py positions",
        "python3 delta_api.py positions BTCUSD",
        "python3 delta_api.py positions BTCUSD --raw",
    ])
    p.add_argument("symbol", nargs="?")
    p.add_argument("--raw", action="store_true", help="print raw JSON instead of the table")

    p = _sub(sub, "orders", "open orders", [
        "python3 delta_api.py orders",
        "python3 delta_api.py orders BTCUSD",
        "python3 delta_api.py orders BTCUSD --raw",
    ])
    p.add_argument("symbol", nargs="?")
    p.add_argument("--raw", action="store_true", help="print raw JSON instead of the table")

    p = _sub(sub, "max-contracts", "how many contracts you can afford, by leverage", [
        "python3 delta_api.py max-contracts BTCUSD",
        "python3 delta_api.py max-contracts BTCUSD --leverage 10 --leverage 50",
        "python3 delta_api.py max-contracts BTCUSD --raw",
    ])
    p.add_argument("symbol")
    p.add_argument("--leverage", type=float, action="append", help="repeatable; defaults to a standard ladder")
    p.add_argument("--raw", action="store_true", help="print raw JSON instead of the table")

    p = _sub(sub, "buy", "open/add to a long", [
        "python3 delta_api.py buy BTCUSD 1",
        "python3 delta_api.py buy BTCUSD 1 --price 80000",
        "python3 delta_api.py buy BTCUSD 1 --sl 80000 --tp 90000",
    ])
    p.add_argument("symbol")
    p.add_argument("size", type=int)
    p.add_argument("--price", type=float, help="limit price; omit for a market order")
    p.add_argument("--sl", type=float, dest="stop_loss", metavar="PRICE", help="attach a stop-loss")
    p.add_argument("--tp", type=float, dest="take_profit", metavar="PRICE", help="attach a take-profit")

    p = _sub(sub, "sell", "open/add to a short", [
        "python3 delta_api.py sell BTCUSD 1",
        "python3 delta_api.py sell BTCUSD 1 --price 90000",
        "python3 delta_api.py sell BTCUSD 1 --sl 90000 --tp 80000",
    ])
    p.add_argument("symbol")
    p.add_argument("size", type=int)
    p.add_argument("--price", type=float, help="limit price; omit for a market order")
    p.add_argument("--sl", type=float, dest="stop_loss", metavar="PRICE", help="attach a stop-loss")
    p.add_argument("--tp", type=float, dest="take_profit", metavar="PRICE", help="attach a take-profit")

    p = _sub(sub, "stop-loss", "attach a stand-alone stop-loss to an existing position", [
        "python3 delta_api.py stop-loss BTCUSD sell 1 80000   # protect a long",
        "python3 delta_api.py stop-loss BTCUSD buy 1 90000    # protect a short",
    ])
    p.add_argument("symbol")
    p.add_argument("side", choices=["buy", "sell"], help="closing side: 'sell' to protect a long, 'buy' a short")
    p.add_argument("size", type=int)
    p.add_argument("stop_price", type=float)

    p = _sub(sub, "take-profit", "attach a stand-alone take-profit to an existing position", [
        "python3 delta_api.py take-profit BTCUSD sell 1 90000   # protect a long",
        "python3 delta_api.py take-profit BTCUSD buy 1 80000    # protect a short",
    ])
    p.add_argument("symbol")
    p.add_argument("side", choices=["buy", "sell"], help="closing side: 'sell' to protect a long, 'buy' a short")
    p.add_argument("size", type=int)
    p.add_argument("stop_price", type=float)

    p = _sub(sub, "trailing-stop", "attach a trailing stop-loss to an existing position", [
        "python3 delta_api.py trailing-stop BTCUSD sell 1 500   # trail 500 USD behind a long's peak",
        "python3 delta_api.py trailing-stop BTCUSD buy 1 500    # trail 500 USD behind a short's trough",
    ])
    p.add_argument("symbol")
    p.add_argument("side", choices=["buy", "sell"], help="closing side: 'sell' to protect a long, 'buy' a short")
    p.add_argument("size", type=int)
    p.add_argument("trail_amount", type=float, help="price distance the stop trails behind the best price, e.g. 500")

    p = _sub(sub, "close", "flatten a position at market", [
        "python3 delta_api.py close BTCUSD",
    ])
    p.add_argument("symbol")

    p = _sub(sub, "cancel", "cancel one open order", [
        "python3 delta_api.py cancel BTCUSD 1575420905",
    ])
    p.add_argument("symbol")
    p.add_argument("order_id", type=int)

    p = _sub(sub, "cancel-all", "cancel all open orders (optionally for one symbol)", [
        "python3 delta_api.py cancel-all",
        "python3 delta_api.py cancel-all BTCUSD",
    ])
    p.add_argument("symbol", nargs="?")

    p = _sub(sub, "cancel-trailing-stop", "cancel the open trailing stop order(s) on a symbol", [
        "python3 delta_api.py cancel-trailing-stop BTCUSD",
    ])
    p.add_argument("symbol")

    p = _sub(sub, "leverage", "get or set leverage for a product", [
        "python3 delta_api.py leverage BTCUSD          # show current leverage",
        "python3 delta_api.py leverage BTCUSD 25        # set leverage to 25x",
    ])
    p.add_argument("symbol")
    p.add_argument("value", type=float, nargs="?", help="omit to just show the current leverage")

    return parser


def main(argv: list[str] | None = None) -> None:
    args = _build_arg_parser().parse_args(argv)

    if args.command == "status":
        print(f"env={ENV} base_url={BASE_URL} api_key_set={bool(API_KEY)}")
        _print_balances_summary(get_balances())
        _print_orders_table(get_open_orders())
        print("Leverage:")
        for symbol in _symbols_in_play():
            try:
                print(f"  {symbol}: {get_leverage(symbol):g}x")
            except DeltaAPIError as exc:
                print(f"  {symbol}: error ({exc.payload})")

    elif args.command == "price":
        print(get_current_price(args.symbol))

    elif args.command == "adx":
        result = is_market_ranging(args.symbol, resolution=args.resolution, period=args.period, threshold=args.threshold)
        if result["is_ranging"]:
            verdict = _color("RANGING - skip signals", "yellow", "bold")
        else:
            verdict = _color("TRENDING - signals OK", "green", "bold")
        print(
            f"{result['symbol']} [{result['resolution']}] ADX({result['period']}) = "
            f"{result['adx']:.2f}  ->  {verdict}  (threshold={result['threshold']:g})"
        )

    elif args.command == "trend":
        result = get_trend(args.symbol, resolution=args.resolution, period=args.period, threshold=args.threshold)
        direction_label = _color("UP", "green", "bold") if result["direction"] == "up" else _color("DOWN", "red", "bold")
        strength_label = (
            _color("trending", "green") if result["is_trending"] else _color("weak/ranging", "yellow")
        )
        print(
            f"{result['symbol']} [{result['resolution']}] +DI={result['plus_di']:.2f}  -DI={result['minus_di']:.2f}  "
            f"ADX({result['period']})={result['adx']:.2f}"
        )
        print(f"  Direction: {direction_label}   Strength: {strength_label}  (threshold={result['threshold']:g})")

    elif args.command == "rsi":
        result = get_rsi_reading(
            args.symbol, resolution=args.resolution, period=args.period, overbought=args.overbought, oversold=args.oversold
        )
        if result["zone"] == "overbought":
            zone_label = _color("OVERBOUGHT", "red", "bold")
        elif result["zone"] == "oversold":
            zone_label = _color("OVERSOLD", "green", "bold")
        else:
            zone_label = _color("NEUTRAL", "yellow")
        print(
            f"{result['symbol']} [{result['resolution']}] RSI({result['period']}) = {result['rsi']:.2f}  ->  {zone_label}  "
            f"(oversold<={result['oversold']:g}, overbought>={result['overbought']:g})"
        )

    elif args.command == "analysis":
        if args.resolution == "all":
            resolutions = sorted(_RESOLUTION_SECONDS, key=_RESOLUTION_SECONDS.get)
        elif args.resolution in _RESOLUTION_SECONDS:
            resolutions = [args.resolution]
        else:
            raise ValueError(f"resolution must be 'all' or one of {sorted(_RESOLUTION_SECONDS)}")
        result = analyze_symbol(
            args.symbol,
            resolutions=resolutions,
            period=args.period,
            adx_threshold=args.threshold,
            overbought=args.overbought,
            oversold=args.oversold,
        )
        if args.raw:
            _print_result("Analysis", result)
        else:
            _print_analysis_table(result)

    elif args.command == "balance":
        balances = get_balances(args.asset)
        if args.raw:
            _print_result("Balances", balances)
        else:
            _print_balances_summary(balances)

    elif args.command == "positions":
        positions = get_positions_pnl(args.symbol)
        if args.raw:
            _print_result("Positions", positions)
        else:
            _print_positions_table(positions)

    elif args.command == "orders":
        orders = get_open_orders(args.symbol)
        if args.raw:
            _print_result("Open orders", orders)
        else:
            _print_orders_table(orders)

    elif args.command == "max-contracts":
        rows = get_max_contracts(args.symbol, leverages=args.leverage)
        if args.raw:
            _print_result("Max contracts", rows)
        else:
            _print_max_contracts_table(args.symbol, rows)

    elif args.command in ("buy", "sell"):
        if args.stop_loss is not None or args.take_profit is not None:
            result = place_bracket_order(
                args.symbol,
                side=args.command,
                size=args.size,
                order_type="limit_order" if args.price is not None else "market_order",
                limit_price=args.price,
                stop_loss_price=args.stop_loss,
                take_profit_price=args.take_profit,
            )
        elif args.price is not None:
            result = place_order(args.symbol, args.command, args.size, order_type="limit_order", limit_price=args.price)
        else:
            result = place_order(args.symbol, args.command, args.size, order_type="market_order")
        _print_order_table(result, "Order Placed")

    elif args.command == "stop-loss":
        result = set_stop_loss(args.symbol, args.side, args.size, args.stop_price)
        _print_order_table(result, "Stop-Loss Placed")

    elif args.command == "take-profit":
        result = set_take_profit(args.symbol, args.side, args.size, args.stop_price)
        _print_order_table(result, "Take-Profit Placed")

    elif args.command == "trailing-stop":
        result = set_trailing_stop_loss(args.symbol, args.side, args.size, args.trail_amount)
        _print_order_table(result, "Trailing Stop Placed")

    elif args.command == "close":
        result = close_position(args.symbol)
        if result is None:
            print("No open position.")
        else:
            _print_order_table(result, "Position Closed")

    elif args.command == "cancel":
        result = cancel_order(args.symbol, args.order_id)
        _print_order_table(result, "Order Cancelled")

    elif args.command == "cancel-all":
        result = cancel_all_orders(args.symbol)
        if isinstance(result, list):
            _print_orders_table(result, "Orders Cancelled")
        else:
            _print_result("Cancelled all", result)

    elif args.command == "cancel-trailing-stop":
        results = cancel_trailing_stop(args.symbol)
        if results:
            _print_orders_table(results, "Trailing Stop(s) Cancelled")
        else:
            print("No trailing stop order open.")

    elif args.command == "leverage":
        if args.value is None:
            print(f"{args.symbol.upper()}: {get_leverage(args.symbol):g}x")
        else:
            _print_result("Leverage set", set_leverage(args.symbol, args.value))


if __name__ == "__main__":
    try:
        main()
    except DeltaAPIError as exc:
        print(f"Delta API error: {exc.payload}", file=sys.stderr)
        sys.exit(1)
    except (RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
