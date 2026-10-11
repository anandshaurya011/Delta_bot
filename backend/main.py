"""
FastAPI backend for delta_api.py (Delta Exchange India).

Every endpoint requires  Authorization: Bearer <Clerk session token>  (the Next.js frontend attaches it).
Set CLERK_PUBLISHABLE_KEY in backend/.env. Each user's Delta keys are saved via PUT /settings/delta.
Run:  .venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000
NOTE: order endpoints place REAL orders on the calling user's Delta account.
"""

from __future__ import annotations

import functools
import json
import sys
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv(Path(__file__).resolve().parent / ".env")
import ai_chat  # noqa: E402
import ai_chat_stream  # noqa: E402
import ai_pipeline  # noqa: E402
import auth  # noqa: E402
import delta_api as delta  # noqa: E402
import store  # noqa: E402

app = FastAPI(
    title="Delta Exchange India Backend",
    description="REST wrapper over delta_api.py. Authenticate with a Clerk session token (`Authorization: Bearer ...`).",
    version="2.0.0",
    dependencies=[Depends(auth.bind_user)],
)

# TradingView calls this directly over the internet and cannot send a Clerk session
# token, so its one route is mounted on a separate sub-app with none of the parent
# app's global Clerk dependency. External URL stays /webhook/tv/{token} either way.
webhook_app = FastAPI(title="TradingView webhook (token-authed, not Clerk-authed)")
app.mount("/webhook", webhook_app)


@app.exception_handler(delta.DeltaAPIError)
async def _delta_error(_: Request, exc: delta.DeltaAPIError):
    return JSONResponse(status_code=502, content={"detail": "Delta API error", "delta_status": exc.status, "delta_error": exc.payload})


@app.exception_handler(delta.CredentialsMissing)
async def _creds_missing(_: Request, exc: delta.CredentialsMissing):
    return JSONResponse(status_code=412, content={"detail": "delta_credentials_missing"})


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
                store.log_event(auth.user_id_var.get() or "unknown", action, req, False, str(getattr(exc, "payload", exc)))
                raise
            store.log_event(auth.user_id_var.get() or "unknown", action, req, True, result)
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

@app.get("/auth/check", tags=["meta"], summary="Validate the session token (no Delta call)")
def auth_check() -> dict[str, bool]:
    return {"ok": True}


@app.get("/status", tags=["meta"], summary="Environment, credentials and account sanity check")
def status() -> dict[str, Any]:
    return {"env": delta.active_env(), "delta_credentials_set": store.get_credentials(auth.user_id_var.get()) is not None,
            "balances": delta.get_balances(), "open_orders": delta.get_open_orders()}


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

@app.get("/history", tags=["meta"], summary="Audit log of the caller's actions")
def history(limit: int = Query(100, ge=1, le=1000), action: str | None = None):
    return store.list_events(auth.user_id_var.get(), limit, action)


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
# Settings
# --------------------------------------------------------------------------

class DeltaCredsIn(BaseModel):
    api_key: str
    api_secret: str
    env: Literal["india_prod", "india_testnet"] = "india_prod"


def _creds_view(uid: str) -> dict[str, Any]:
    c = store.get_credentials(uid)
    return {"is_set": True, "env": c.env, "api_key_masked": "••••" + c.api_key[-4:]} if c else {"is_set": False, "env": None, "api_key_masked": None}


@app.get("/settings/delta", tags=["settings"], summary="Whether Delta keys are saved (never returns the secret)")
def get_delta_settings():
    return _creds_view(auth.user_id_var.get())


@app.put("/settings/delta", tags=["settings"], summary="Validate and save the caller's Delta API keys")
def put_delta_settings(body: DeltaCredsIn):
    uid = auth.user_id_var.get()
    tok = delta.use_credentials(body.api_key.strip(), body.api_secret.strip(), body.env)
    try:
        delta.get_balances()                       # cheap authenticated call proves the keys work for this env
    except delta.DeltaAPIError as exc:
        raise HTTPException(400, f"Delta rejected these credentials ({body.env}): {exc.payload}") from None
    finally:
        delta._CREDS.reset(tok)
    store.save_credentials(uid, body.api_key.strip(), body.api_secret.strip(), body.env)
    return _creds_view(uid)


@app.delete("/settings/delta", tags=["settings"], summary="Remove the caller's saved Delta keys")
def delete_delta_settings():
    store.delete_credentials(auth.user_id_var.get())
    return {"is_set": False}


# --------------------------------------------------------------------------
# AI settings, signals, and TradingView webhook
# --------------------------------------------------------------------------

class AiSettingsIn(BaseModel):
    enabled: bool | None = None
    cooldown_minutes: int | None = None
    coordinator_model: str | None = None
    data_model: str | None = None
    executer_model: str | None = None
    coordinator_prompt: str | None = None
    data_prompt: str | None = None
    executer_prompt: str | None = None
    guardrails_json: dict[str, Any] | None = None
    paper_trading: bool | None = None


class WebhookSignalIn(BaseModel):
    symbol: str
    timeframe: str
    action: str
    note: str | None = None


@app.get("/ai/settings", tags=["ai"])
def get_ai_settings():
    return store.get_or_create_ai_settings(auth.user_id_var.get())


@app.put("/ai/settings", tags=["ai"])
def put_ai_settings(body: AiSettingsIn):
    user_id = auth.user_id_var.get()
    store.get_or_create_ai_settings(user_id)
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    return store.update_ai_settings(user_id, **fields)


@app.post("/ai/webhook/regenerate", tags=["ai"])
def regenerate_ai_webhook():
    token = store.regenerate_webhook_token(auth.user_id_var.get())
    return {"webhook_token": token}


@app.get("/ai/signals", tags=["ai"])
def get_ai_signals(limit: int = Query(50, ge=1, le=500)):
    return store.list_signals(auth.user_id_var.get(), limit)


@app.get("/ai/paper-trades", tags=["ai"])
def get_ai_paper_trades(limit: int = Query(100, ge=1, le=500)):
    return store.list_paper_trades(auth.user_id_var.get(), limit)


@app.get("/ai/paper-trades/summary", tags=["ai"])
def get_ai_paper_trades_summary():
    return store.get_paper_pnl_summary(auth.user_id_var.get())


class ClosePaperTradeIn(BaseModel):
    exit_price: float | None = None


@app.post("/ai/paper-trades/{trade_id}/close", tags=["ai"])
def post_close_paper_trade(trade_id: int, body: ClosePaperTradeIn):
    trades = store.list_paper_trades(auth.user_id_var.get())
    trade = next((t for t in trades if t["id"] == trade_id), None)
    if not trade:
        raise HTTPException(404, "Unknown paper trade")
    exit_price = body.exit_price if body.exit_price is not None else delta.get_current_price(trade["symbol"])
    closed = store.close_paper_trade(trade_id, exit_price)
    if not closed:
        raise HTTPException(409, "Trade already closed")
    return closed


def _run_signal_pipeline(signal_id: int, user_id: str, signal: dict[str, Any]) -> None:
    settings = store.get_or_create_ai_settings(user_id)
    try:
        result = ai_pipeline.run_pipeline(user_id, signal, settings, signal_id=signal_id)
        store.update_signal_result(signal_id, "done", result)
        if result["execution_result"].get("reason") == "guardrail:max_daily_loss":
            store.update_ai_settings(user_id, enabled=False)
    except Exception as exc:  # pragma: no cover - defensive, exercised via mocked failure in review
        store.update_signal_result(signal_id, "error", {"error": str(exc)})


@webhook_app.post("/tv/{token}", tags=["ai"])
def tradingview_webhook(token: str, body: WebhookSignalIn, background_tasks: BackgroundTasks):
    user_id = store.get_user_id_by_webhook_token(token)
    if not user_id:
        raise HTTPException(404, "Unknown or disabled webhook")
    settings = store.get_or_create_ai_settings(user_id)
    outcome = store.record_incoming_signal(
        user_id, body.symbol, body.timeframe, body.action, body.note, settings["cooldown_minutes"]
    )
    if outcome["status"] == "processing":
        signal = {"symbol": body.symbol, "timeframe": body.timeframe, "action": body.action, "note": body.note}
        background_tasks.add_task(_run_signal_pipeline, outcome["id"], user_id, signal)
    return {"status": outcome["status"]}


class ChatMessageIn(BaseModel):
    message: str = Field(min_length=1)
    symbol: str | None = None


def _run_chat_message(user_id: str, message: str, settings: dict[str, Any], symbol: str | None = None) -> None:
    def on_event(event: dict[str, Any]) -> None:
        # Hardening (Task 4 review carryover): on_event runs INSIDE handle_chat_message's
        # trade-path try/except. ai_chat_stream.publish() can raise queue.Full under rare
        # concurrent contention; letting that escape here would get caught by that
        # except and misrecord a successful trade as an "error" outcome. Swallow it --
        # a degraded live-thinking stream for one event is an acceptable trade-off.
        try:
            ai_chat_stream.publish(user_id, event)
        except Exception:
            pass

    ai_chat.handle_chat_message(user_id, message, settings, on_event=on_event, symbol=symbol)


@app.post("/ai/chat", tags=["ai"])
def post_ai_chat(body: ChatMessageIn, background_tasks: BackgroundTasks):
    user_id = auth.user_id_var.get()
    settings = store.get_or_create_ai_settings(user_id)
    background_tasks.add_task(_run_chat_message, user_id, body.message, settings, body.symbol)
    return {"status": "processing"}


@app.get("/ai/chat/stream", tags=["ai"])
def get_ai_chat_stream():
    user_id = auth.user_id_var.get()
    ai_chat_stream.clear_stale(user_id)

    def event_source():
        for event in ai_chat_stream.drain(user_id):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(event_source(), media_type="text/event-stream")


@app.get("/ai/chat/messages", tags=["ai"])
def get_ai_chat_messages(limit: int = Query(100, ge=1, le=500)):
    return store.list_chat_messages(auth.user_id_var.get(), limit)
