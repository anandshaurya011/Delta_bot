"""
SQLite audit log for the TradingView -> coordinator -> executor pipeline.

Four tables, matching the pipeline's four stages:
    signals            - every inbound webhook event (for idempotency)
    market_snapshots   - the data-agent JSON the coordinator decided against
    decisions          - the coordinator's BUY/SELL/IGNORE call + reasons
    decisions.trade_plan_json

    executions         - every call into execute_trade.py (DRY_RUN or live)

Shared by log_event.py (coordinator-side logging) and execute_trade.py
(executor-side logging + risk checks). No secrets are ever written here -
only symbols, sides, prices, sizes, and decision/result text.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parent / "trade_events.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    event_id    TEXT PRIMARY KEY,
    received_at TEXT NOT NULL,
    source      TEXT,
    strategy    TEXT,
    symbol      TEXT,
    signal      TEXT,
    entry       REAL,
    sl          REAL,
    tp1         REAL,
    tp2         REAL,
    tp3         REAL,
    timeframe   TEXT,
    raw_payload TEXT,
    status      TEXT NOT NULL DEFAULT 'PROCESS'
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id      TEXT NOT NULL,
    captured_at   TEXT NOT NULL,
    snapshot_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT NOT NULL,
    decided_at      TEXT NOT NULL,
    decision        TEXT NOT NULL,
    reason_json     TEXT,
    trade_plan_json TEXT
);

CREATE TABLE IF NOT EXISTS executions (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id              TEXT,
    requested_at           TEXT NOT NULL,
    action                 TEXT NOT NULL,
    symbol                 TEXT,
    side                   TEXT,
    quantity               REAL,
    entry                  REAL,
    stop_loss              REAL,
    tp1                    REAL,
    tp2                    REAL,
    tp3                    REAL,
    trading_mode           TEXT NOT NULL,
    result_status          TEXT NOT NULL,
    rejection_reasons_json TEXT,
    order_result_json      TEXT,
    realized_pnl           REAL
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def record_signal(event_id: str, payload: dict[str, Any], status: str = "PROCESS") -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO signals
               (event_id, received_at, source, strategy, symbol, signal, entry, sl, tp1, tp2, tp3, timeframe, raw_payload, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id, _now(), payload.get("source"), payload.get("strategy"), payload.get("symbol"),
                payload.get("signal"), payload.get("entry"), payload.get("sl"), payload.get("tp1"),
                payload.get("tp2"), payload.get("tp3"), payload.get("timeframe"), json.dumps(payload), status,
            ),
        )


def is_duplicate_signal(event_id: str) -> bool:
    with _connect() as conn:
        row = conn.execute("SELECT 1 FROM signals WHERE event_id = ?", (event_id,)).fetchone()
        return row is not None


def record_market_snapshot(event_id: str, snapshot: dict[str, Any]) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO market_snapshots (event_id, captured_at, snapshot_json) VALUES (?, ?, ?)",
            (event_id, _now(), json.dumps(snapshot)),
        )


def record_decision(event_id: str, decision: str, reasons: list[str], trade_plan: dict[str, Any] | None) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO decisions (event_id, decided_at, decision, reason_json, trade_plan_json) VALUES (?, ?, ?, ?, ?)",
            (event_id, _now(), decision, json.dumps(reasons), json.dumps(trade_plan) if trade_plan else None),
        )


def record_execution(
    signal_id: str | None,
    action: str,
    symbol: str | None,
    side: str | None,
    quantity: float | None,
    entry: float | None,
    stop_loss: float | None,
    tp1: float | None,
    tp2: float | None,
    tp3: float | None,
    trading_mode: str,
    result_status: str,
    rejection_reasons: list[str] | None = None,
    order_result: dict[str, Any] | None = None,
    realized_pnl: float | None = None,
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO executions
               (signal_id, requested_at, action, symbol, side, quantity, entry, stop_loss, tp1, tp2, tp3,
                trading_mode, result_status, rejection_reasons_json, order_result_json, realized_pnl)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                signal_id, _now(), action, symbol, side, quantity, entry, stop_loss, tp1, tp2, tp3,
                trading_mode, result_status,
                json.dumps(rejection_reasons) if rejection_reasons else None,
                json.dumps(order_result) if order_result is not None else None,
                realized_pnl,
            ),
        )
        return cur.lastrowid


def has_executed_signal(signal_id: str) -> bool:
    """True if signal_id already has an EXECUTED or VALIDATED_DRY_RUN OPEN_POSITION execution."""
    with _connect() as conn:
        row = conn.execute(
            """SELECT 1 FROM executions
               WHERE signal_id = ? AND action = 'OPEN_POSITION'
                 AND result_status IN ('EXECUTED', 'VALIDATED_DRY_RUN')
               LIMIT 1""",
            (signal_id,),
        ).fetchone()
        return row is not None


def todays_realized_pnl() -> float:
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    with _connect() as conn:
        row = conn.execute(
            """SELECT COALESCE(SUM(realized_pnl), 0) AS total FROM executions
               WHERE realized_pnl IS NOT NULL AND requested_at >= ?""",
            (today,),
        ).fetchone()
        return float(row["total"])
