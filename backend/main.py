"""
FastAPI backend for delta_api.py (Delta Exchange India).

Every endpoint requires the header  X-API-Key: <key>.  The key is a random
secret: set BACKEND_API_KEY in the environment or in backend/.env; if neither
exists, one is generated on first start, saved to backend/.env (chmod 600) and
printed to the console.

Run:
    cd /home/anand-shaurya/Documents/personal/delta/backend
    .venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000

Interactive docs: http://127.0.0.1:8000/docs  -> click "Authorize", paste the key.
NOTE: order endpoints place REAL orders on the account configured in delta_api.
"""

from __future__ import annotations

import functools
import os
import secrets
import sys
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402
import delta_api as delta  # noqa: E402

# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------

_ENV_PATH = Path(__file__).resolve().parent / ".env"


def _load_backend_key() -> str:
    key = os.environ.get("BACKEND_API_KEY", "").strip()
    if key:
        return key
    if _ENV_PATH.is_file():
        for line in _ENV_PATH.read_text().splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "BACKEND_API_KEY" and value.strip():
                return value.strip().strip('"').strip("'")
    key = secrets.token_urlsafe(32)
    fd = os.open(_ENV_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(f"BACKEND_API_KEY={key}\n")
    print(f"\n*** Generated new API key (saved to {_ENV_PATH}):\n    {key}\n", flush=True)
    return key


BACKEND_API_KEY = _load_backend_key()
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False, description="Backend API key")


def require_api_key(key: str | None = Depends(_api_key_header)) -> None:
    if not key or not secrets.compare_digest(key.encode(), BACKEND_API_KEY.encode()):
        raise HTTPException(status_code=401, detail="Invalid or missing API key", headers={"WWW-Authenticate": "ApiKey"})


app = FastAPI(
    title="Delta Exchange India Backend",
    description="REST wrapper over delta_api.py. Authenticate with the `X-API-Key` header (use **Authorize** above).",
    version="1.0.0",
    dependencies=[Depends(require_api_key)],
)


@app.exception_handler(delta.DeltaAPIError)
async def _delta_error(_: Request, exc: delta.DeltaAPIError):
    return JSONResponse(status_code=502, content={"detail": "Delta API error", "delta_status": exc.status, "delta_error": exc.payload})


@app.exception_handler(ValueError)
async def _value_error(_: Request, exc: ValueError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(RuntimeError)
async def _runtime_error(_: Request, exc: RuntimeError):
    return JSONResponse(status_code=500, content={"detail": str(exc)})


def audited(action: str):
    """Record every call (success or failure) of a state-changing endpoint in the JSON DB."""

    def wrap(fn):
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            req = {k: (v.model_dump() if isinstance(v, BaseModel) else v) for k, v in kwargs.items()}
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                db.log_event(action, req, False, str(getattr(exc, "payload", exc)))
                raise
            db.log_event(action, req, True, result)
            return result

        return inner

    return wrap


Resolution = Literal["5s", "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "1d", "1w"]
Side = Literal["buy", "sell"]


# --------------------------------------------------------------------------
# Request bodies
# --------------------------------------------------------------------------

class TradeIn(BaseModel):
    symbol: str = Field(examples=["BTCUSD"])
    size: int = Field(gt=0, description="Contracts, not USD")
    price: float | None = Field(None, description="Limit price; omit for a market order")
    stop_loss: float | None = Field(None, description="Attach a stop-loss at this price")
    take_profit: float | None = Field(None, description="Attach a take-profit at this price")


class StopIn(BaseModel):
    symbol: str = Field(examples=["BTCUSD"])
    side: Side = Field(description="CLOSING side: 'sell' protects a long, 'buy' protects a short")
    size: int = Field(gt=0)
    stop_price: float


class TrailingStopIn(BaseModel):
    symbol: str = Field(examples=["BTCUSD"])
    side: Side
    size: int = Field(gt=0)
    trail_amount: float = Field(gt=0, description="Price distance the stop trails behind the best price")


class BracketEditIn(BaseModel):
    symbol: str = Field(examples=["BTCUSD"])
    stop_loss: float | None = None
    take_profit: float | None = None
    trail_amount: float | None = Field(None, gt=0)


class LeverageIn(BaseModel):
    leverage: float = Field(gt=0)


# --------------------------------------------------------------------------
# Meta / market data
# --------------------------------------------------------------------------

@app.get("/auth/check", tags=["meta"], summary="Validate the API key (no Delta call)")
def auth_check() -> dict[str, bool]:
    return {"ok": True}


@app.get("/status", tags=["meta"], summary="Environment, credentials and account sanity check")
def status() -> dict[str, Any]:
    return {
        "env": delta.ENV,
        "base_url": delta.BASE_URL,
        "delta_credentials_set": bool(delta.API_KEY and delta.API_SECRET),
        "balances": delta.get_balances(),
        "open_orders": delta.get_open_orders(),
    }


@app.get("/products", tags=["market"], summary="List products")
def products(contract_types: str | None = None, states: str = "live"):
    return delta.get_products(contract_types, states)


@app.get("/products/{symbol}", tags=["market"], summary="One product")
def product(symbol: str):
    return delta.get_product(symbol)


@app.get("/ticker/{symbol}", tags=["market"], summary="Raw ticker")
def ticker(symbol: str):
    return delta.get_ticker(symbol)


@app.get("/price/{symbol}", tags=["market"], summary="Current mark price")
def price(symbol: str):
    return {"symbol": symbol.upper(), "price": delta.get_current_price(symbol)}


@app.get("/candles/{symbol}", tags=["market"], summary="OHLC candles, oldest first")
def candles(symbol: str, resolution: Resolution = "1h", count: int = Query(200, ge=1, le=2000)):
    return delta.get_candles(symbol, resolution=resolution, count=count)


# --------------------------------------------------------------------------
# Indicators
# --------------------------------------------------------------------------

@app.get("/indicators/adx/{symbol}", tags=["indicators"], summary="ADX: ranging vs trending")
def adx(symbol: str, resolution: Resolution = "1h", period: int = Query(14, ge=2), threshold: float = 25):
    return delta.is_market_ranging(symbol, resolution=resolution, period=period, threshold=threshold)


@app.get("/indicators/trend/{symbol}", tags=["indicators"], summary="Trend direction + strength (DMI/ADX)")
def trend(symbol: str, resolution: Resolution = "1h", period: int = Query(14, ge=2), threshold: float = 25):
    return delta.get_trend(symbol, resolution=resolution, period=period, threshold=threshold)


@app.get("/indicators/rsi/{symbol}", tags=["indicators"], summary="RSI with overbought/oversold zone")
def rsi(
    symbol: str,
    resolution: Resolution = "1h",
    period: int = Query(14, ge=2),
    overbought: float = 70,
    oversold: float = 30,
):
    return delta.get_rsi_reading(symbol, resolution=resolution, period=period, overbought=overbought, oversold=oversold)


@app.get("/indicators/atr/{symbol}", tags=["indicators"], summary="ATR (volatility)")
def atr(symbol: str, resolution: Resolution = "1h", period: int = Query(14, ge=2)):
    return {
        "symbol": symbol.upper(),
        "resolution": resolution,
        "period": period,
        "atr": delta.get_atr(symbol, resolution=resolution, period=period),
    }


@app.get("/indicators/structure/{symbol}", tags=["indicators"], summary="Market structure (HH/LH/HL/LL)")
def structure(symbol: str, resolution: Resolution = "1h", swing_strength: int = Query(2, ge=1)):
    return {
        "symbol": symbol.upper(),
        "resolution": resolution,
        "structure": delta.get_market_structure(symbol, resolution=resolution, swing_strength=swing_strength),
    }


@app.get("/analysis/{symbol}", tags=["indicators"], summary="Combined multi-timeframe analysis")
def analysis(
    symbol: str,
    resolutions: list[Resolution] | None = Query(None, description="Repeatable; omit for every timeframe"),
    period: int = Query(14, ge=2),
    threshold: float = 25,
    overbought: float = 70,
    oversold: float = 30,
):
    return delta.analyze_symbol(
        symbol, resolutions=resolutions, period=period, adx_threshold=threshold, overbought=overbought, oversold=oversold
    )


# --------------------------------------------------------------------------
# Account
# --------------------------------------------------------------------------

@app.get("/balances", tags=["account"], summary="Wallet balances (optionally one asset)")
def balances(asset: str | None = None):
    return delta.get_balances(asset)


@app.get("/positions", tags=["account"], summary="Open positions with PnL")
def positions(symbol: str | None = None):
    return delta.get_positions_pnl(symbol)


@app.get("/positions/pnl/total", tags=["account"], summary="Total unrealized PnL")
def total_pnl():
    return {"total_unrealized_pnl": delta.get_total_unrealized_pnl()}


@app.get("/max-contracts/{symbol}", tags=["account"], summary="Affordable contracts per leverage level")
def max_contracts(
    symbol: str,
    leverage: list[float] | None = Query(None, description="Repeatable; omit for a standard ladder"),
    price: float | None = Query(None, gt=0, description="Entry price to assume; defaults to the current mark price"),
):
    """
    Each row also carries ESTIMATED isolated-margin liquidation prices for a position opened at `price`:
      long  = price * (1 - 1/leverage + maintenance_margin)
      short = price * (1 + 1/leverage - maintenance_margin)
    Fees, funding and Delta's size-based margin tiers are ignored, so treat it as an approximation.
    """
    entry = price if price is not None else delta.get_current_price(symbol)
    mm = float(delta.get_product(symbol).get("maintenance_margin") or 0) / 100
    rows = delta.get_max_contracts(symbol, leverages=leverage, price=entry)
    for row in rows:
        inv = 1 / row["leverage"]
        row["entry_price"] = entry
        row["maintenance_margin_rate"] = mm
        row["long_liq_price"] = max(0.0, entry * (1 - inv + mm))
        row["short_liq_price"] = entry * (1 + inv - mm)
        row["liq_distance_pct"] = max(0.0, (inv - mm) * 100)
    return rows


@app.get("/leverage/{symbol}", tags=["account"], summary="Current leverage")
def get_leverage(symbol: str):
    return {"symbol": symbol.upper(), "leverage": delta.get_leverage(symbol)}


@app.put("/leverage/{symbol}", tags=["account"], summary="Set leverage")
@audited("set-leverage")
def set_leverage(symbol: str, body: LeverageIn):
    return delta.set_leverage(symbol, body.leverage)


# --------------------------------------------------------------------------
# Orders (REAL orders)
# --------------------------------------------------------------------------

@app.get("/history", tags=["meta"], summary="Audit log of actions taken through this backend")
def history(limit: int = Query(100, ge=1, le=1000), action: str | None = None):
    return db.list_events(limit, action)


@app.get("/orders/history", tags=["orders"], summary="Past orders (filled/cancelled), newest first")
def order_history(symbol: str | None = None, limit: int = Query(50, ge=1, le=200)):
    query: dict[str, Any] = {"page_size": limit}
    if symbol:
        query["product_ids"] = str(delta.get_product_id(symbol))
    return delta._request("GET", "/v2/orders/history", query=query, auth=True)


@app.get("/fills", tags=["orders"], summary="Trade fills, newest first")
def fills(symbol: str | None = None, limit: int = Query(50, ge=1, le=200)):
    query: dict[str, Any] = {"page_size": limit}
    if symbol:
        query["product_ids"] = str(delta.get_product_id(symbol))
    return delta._request("GET", "/v2/fills", query=query, auth=True)


@app.get("/orders", tags=["orders"], summary="Open orders")
def orders(symbol: str | None = None):
    return delta.get_open_orders(symbol)


def _trade(side: str, body: TradeIn):
    if body.stop_loss is not None or body.take_profit is not None:
        return delta.place_bracket_order(
            body.symbol,
            side=side,
            size=body.size,
            order_type="limit_order" if body.price is not None else "market_order",
            limit_price=body.price,
            stop_loss_price=body.stop_loss,
            take_profit_price=body.take_profit,
        )
    if body.price is not None:
        return delta.place_order(body.symbol, side, body.size, order_type="limit_order", limit_price=body.price)
    return delta.place_order(body.symbol, side, body.size, order_type="market_order")


@app.post("/orders/buy", tags=["orders"], summary="Open/add to a long (market, or limit if price given)")
@audited("buy")
def buy(body: TradeIn):
    return _trade("buy", body)


@app.post("/orders/sell", tags=["orders"], summary="Open/add to a short (market, or limit if price given)")
@audited("sell")
def sell(body: TradeIn):
    return _trade("sell", body)


@app.post("/orders/stop-loss", tags=["orders"], summary="Stand-alone stop-loss on an existing position")
@audited("stop-loss")
def stop_loss(body: StopIn):
    return delta.set_stop_loss(body.symbol, body.side, body.size, body.stop_price)


@app.post("/orders/take-profit", tags=["orders"], summary="Stand-alone take-profit on an existing position")
@audited("take-profit")
def take_profit(body: StopIn):
    return delta.set_take_profit(body.symbol, body.side, body.size, body.stop_price)


@app.post("/orders/trailing-stop", tags=["orders"], summary="Trailing stop-loss on an existing position")
@audited("trailing-stop")
def trailing_stop(body: TrailingStopIn):
    return delta.set_trailing_stop_loss(body.symbol, body.side, body.size, body.trail_amount)


@app.post("/positions/{symbol}/close", tags=["orders"], summary="Flatten a position at market")
@audited("close")
def close(symbol: str):
    result = delta.close_position(symbol)
    return result if result is not None else {"detail": "No open position"}


@app.put("/orders/bracket", tags=["orders"], summary="Add/edit SL, TP or trailing on the bracket of an order/position")
@audited("edit-bracket")
def edit_bracket(body: BracketEditIn):
    if body.stop_loss is None and body.take_profit is None and body.trail_amount is None:
        raise ValueError("give at least one of stop_loss, take_profit, trail_amount")
    return delta.edit_bracket_order(
        body.symbol, stop_loss_price=body.stop_loss, take_profit_price=body.take_profit, trail_amount=body.trail_amount
    )


@app.delete("/orders/{symbol}/trailing-stop", tags=["orders"], summary="Cancel trailing stop(s) on a symbol")
@audited("cancel-trailing-stop")
def cancel_trailing(symbol: str):
    return delta.cancel_trailing_stop(symbol)


@app.delete("/orders/{symbol}/{order_id}", tags=["orders"], summary="Cancel one order")
@audited("cancel")
def cancel(symbol: str, order_id: int):
    return delta.cancel_order(symbol, order_id)


@app.delete("/orders", tags=["orders"], summary="Cancel all open orders (optionally one symbol)")
@audited("cancel-all")
def cancel_all(symbol: str | None = None):
    return delta.cancel_all_orders(symbol)


# --------------------------------------------------------------------------
# Web UI (static files are public; every data call from the UI still needs the key)
# --------------------------------------------------------------------------

async def _root(_: Request):
    return RedirectResponse("/ui/")


app.add_route("/", _root, include_in_schema=False)
app.mount("/ui", StaticFiles(directory=Path(__file__).resolve().parent / "static", html=True), name="ui")
