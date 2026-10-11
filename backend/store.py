"""SQLite store: encrypted per-user Delta credentials and a per-user audit log."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import crypto

MAX_EVENTS_PER_USER = 5000
_DEFAULT_PATH = Path(__file__).resolve().parent / "data.db"


@dataclass(frozen=True)
class Creds:
    api_key: str
    api_secret: str
    env: str


def _conn() -> sqlite3.Connection:
    path = Path(os.environ.get("DELTA_DB_PATH") or _DEFAULT_PATH)
    new = not path.exists()
    if new:
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))
    c = sqlite3.connect(path, timeout=10, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("create table if not exists credentials(user_id text primary key, api_key_enc text not null, api_secret_enc text not null, env text not null, updated_at integer not null)")
    c.execute("create table if not exists events(id integer primary key autoincrement, user_id text not null, ts integer not null, action text not null, request text, ok integer not null, result text)")
    c.execute("create index if not exists ev_user on events(user_id, id)")
    c.execute(
        "create table if not exists ai_settings("
        "user_id text primary key, webhook_token text unique not null, enabled integer not null default 0, "
        "cooldown_minutes integer not null default 30, coordinator_model text not null default 'gpt-4.1', "
        "data_model text not null default 'gpt-4.1-mini', executer_model text not null default 'gpt-4.1-mini', "
        "coordinator_prompt text not null default '', data_prompt text not null default '', "
        "executer_prompt text not null default '', guardrails_json text not null default '{}', "
        "updated_at integer not null)"
    )
    c.execute(
        "create table if not exists ai_signals(id integer primary key autoincrement, user_id text not null, "
        "ts integer not null, symbol text not null, timeframe text not null, action text not null, note text, "
        "dedup_key text not null, status text not null, result_json text)"
    )
    c.execute("create index if not exists ai_sig_user on ai_signals(user_id, id)")
    c.execute("create index if not exists ai_sig_cooldown on ai_signals(user_id, timeframe, ts)")
    c.execute("create index if not exists ai_sig_dedup on ai_signals(user_id, dedup_key, ts)")
    c.execute(
        "create table if not exists ai_trade_count(user_id text not null, day text not null, "
        "count integer not null default 0, primary key (user_id, day))"
    )
    c.execute(
        "create table if not exists ai_chat_messages(id integer primary key autoincrement, "
        "user_id text not null, ts integer not null, role text not null, content text not null, "
        "signal_id integer)"
    )
    c.execute("create index if not exists ai_chat_user on ai_chat_messages(user_id, id)")
    return c


def save_credentials(user_id: str, api_key: str, api_secret: str, env: str) -> None:
    with closing(_conn()) as c, c:
        c.execute("insert or replace into credentials values(?,?,?,?,?)",
                  (user_id, crypto.encrypt(api_key), crypto.encrypt(api_secret), env, int(time.time())))


def get_credentials(user_id: str) -> Creds | None:
    with closing(_conn()) as c:
        row = c.execute("select api_key_enc, api_secret_enc, env from credentials where user_id=?", (user_id,)).fetchone()
    return Creds(crypto.decrypt(row[0]), crypto.decrypt(row[1]), row[2]) if row else None


def delete_credentials(user_id: str) -> bool:
    with closing(_conn()) as c, c:
        return c.execute("delete from credentials where user_id=?", (user_id,)).rowcount > 0


def log_event(user_id: str, action: str, request: Any, ok: bool, result: Any) -> None:
    with closing(_conn()) as c, c:
        c.execute("insert into events(user_id, ts, action, request, ok, result) values(?,?,?,?,?,?)",
                  (user_id, int(time.time()), action, json.dumps(request, default=str), int(ok), json.dumps(result, default=str)))
        c.execute("delete from events where user_id=? and id not in (select id from events where user_id=? order by id desc limit ?)",
                  (user_id, user_id, MAX_EVENTS_PER_USER))


def list_events(user_id: str, limit: int = 100, action: str | None = None) -> list[dict[str, Any]]:
    q, args = "select ts, action, request, ok, result from events where user_id=?", [user_id]
    if action:
        q += " and action=?"; args.append(action)
    q += " order by id desc limit ?"; args.append(limit)
    with closing(_conn()) as c:
        rows = c.execute(q, args).fetchall()
    return [{"ts": ts, "action": a, "request": json.loads(rq), "ok": bool(ok), "result": json.loads(rs)} for ts, a, rq, ok, rs in rows]


_AI_SETTINGS_COLS = (
    "user_id", "webhook_token", "enabled", "cooldown_minutes", "coordinator_model",
    "data_model", "executer_model", "coordinator_prompt", "data_prompt", "executer_prompt",
    "guardrails_json", "updated_at",
)


def _ai_settings_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = {k: row[k] for k in _AI_SETTINGS_COLS}
    d["guardrails_json"] = json.loads(d["guardrails_json"])
    return d


def get_or_create_ai_settings(user_id: str) -> dict[str, Any]:
    with closing(_conn()) as c:
        row = c.execute("select * from ai_settings where user_id=?", (user_id,)).fetchone()
        if row:
            return _ai_settings_to_dict(row)
        token = secrets.token_urlsafe(24)
        c.execute(
            "insert into ai_settings(user_id, webhook_token, enabled, cooldown_minutes, coordinator_model, "
            "data_model, executer_model, coordinator_prompt, data_prompt, executer_prompt, guardrails_json, "
            "updated_at) values(?,?,?,?,?,?,?,?,?,?,?,?)",
            (user_id, token, 0, 30, "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-mini", "", "", "", "{}", int(time.time())),
        )
        row = c.execute("select * from ai_settings where user_id=?", (user_id,)).fetchone()
        return _ai_settings_to_dict(row)


def update_ai_settings(user_id: str, **fields: Any) -> dict[str, Any]:
    allowed = set(_AI_SETTINGS_COLS) - {"user_id", "webhook_token"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if "guardrails_json" in sets:
        sets["guardrails_json"] = json.dumps(sets["guardrails_json"])
    sets["updated_at"] = int(time.time())
    with closing(_conn()) as c:
        cols = ", ".join(f"{k}=?" for k in sets)
        c.execute(f"update ai_settings set {cols} where user_id=?", (*sets.values(), user_id))
        row = c.execute("select * from ai_settings where user_id=?", (user_id,)).fetchone()
        return _ai_settings_to_dict(row)


def regenerate_webhook_token(user_id: str) -> str:
    token = secrets.token_urlsafe(24)
    with closing(_conn()) as c:
        c.execute("update ai_settings set webhook_token=?, updated_at=? where user_id=?", (token, int(time.time()), user_id))
    return token


def get_user_id_by_webhook_token(token: str) -> str | None:
    with closing(_conn()) as c:
        row = c.execute("select user_id from ai_settings where webhook_token=? and enabled=1", (token,)).fetchone()
    return row["user_id"] if row else None


AI_DEDUP_WINDOW_SECONDS = 120
# Generous relative to the 10s fill-verification timeout plus LLM call latency, but
# short enough that a crash mid-pipeline (leaving a row stuck at status="processing")
# doesn't permanently block every future signal for that (user_id, timeframe).
AI_PROCESSING_STALE_SECONDS = 120


def _dedup_key(symbol: str, timeframe: str, action: str, note: str | None) -> str:
    raw = f"{symbol}|{timeframe}|{action}|{note or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()


def record_incoming_signal(
    user_id: str, symbol: str, timeframe: str, action: str, note: str | None, cooldown_minutes: int
) -> dict[str, Any]:
    key = _dedup_key(symbol, timeframe, action, note)
    now = int(time.time())
    with closing(_conn()) as c:
        c.execute("begin immediate")
        try:
            dup = c.execute(
                "select id from ai_signals where user_id=? and dedup_key=? and ts > ? limit 1",
                (user_id, key, now - AI_DEDUP_WINDOW_SECONDS),
            ).fetchone()
            inflight = c.execute(
                "select id from ai_signals where user_id=? and timeframe=? and status='processing' and ts > ? limit 1",
                (user_id, timeframe, now - AI_PROCESSING_STALE_SECONDS),
            ).fetchone()
            if dup or inflight:
                status = "duplicate_skipped"
            else:
                last_trade = c.execute(
                    "select ts from ai_signals where user_id=? and timeframe=? and status='done' "
                    "and json_extract(result_json,'$.execution_result.executed')=1 order by ts desc limit 1",
                    (user_id, timeframe),
                ).fetchone()
                if last_trade and now - last_trade["ts"] < cooldown_minutes * 60:
                    status = "cooldown_skipped"
                else:
                    status = "processing"
            cur = c.execute(
                "insert into ai_signals(user_id, ts, symbol, timeframe, action, note, dedup_key, status, result_json) "
                "values(?,?,?,?,?,?,?,?,?)",
                (user_id, now, symbol, timeframe, action, note, key, status, None),
            )
            signal_id = cur.lastrowid
            c.execute("commit")
        except Exception:
            c.execute("rollback")
            raise
    return {"id": signal_id, "status": status}


def update_signal_result(signal_id: int, status: str, result: dict[str, Any]) -> None:
    with closing(_conn()) as c:
        c.execute("update ai_signals set status=?, result_json=? where id=?", (status, json.dumps(result, default=str), signal_id))


def list_signals(user_id: str, limit: int = 50) -> list[dict[str, Any]]:
    with closing(_conn()) as c:
        rows = c.execute(
            "select id, ts, symbol, timeframe, action, note, status, result_json from ai_signals "
            "where user_id=? order by id desc limit ?",
            (user_id, limit),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["result_json"] = json.loads(d["result_json"]) if d["result_json"] else None
        out.append(d)
    return out


def increment_trade_count(user_id: str) -> None:
    day = time.strftime("%Y-%m-%d", time.gmtime())
    with closing(_conn()) as c:
        c.execute(
            "insert into ai_trade_count(user_id, day, count) values(?,?,1) "
            "on conflict(user_id, day) do update set count = count + 1",
            (user_id, day),
        )


def get_trade_count_today(user_id: str) -> int:
    day = time.strftime("%Y-%m-%d", time.gmtime())
    with closing(_conn()) as c:
        row = c.execute("select count from ai_trade_count where user_id=? and day=?", (user_id, day)).fetchone()
    return row["count"] if row else 0


def add_chat_message(user_id: str, role: str, content: str, signal_id: int | None = None) -> int:
    with closing(_conn()) as c:
        cur = c.execute(
            "insert into ai_chat_messages(user_id, ts, role, content, signal_id) values(?,?,?,?,?)",
            (user_id, int(time.time()), role, content, signal_id),
        )
        return cur.lastrowid


def list_chat_messages(user_id: str, limit: int = 100) -> list[dict[str, Any]]:
    with closing(_conn()) as c:
        rows = c.execute(
            "select id, ts, role, content, signal_id from ai_chat_messages "
            "where user_id=? order by id desc limit ?",
            (user_id, limit),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]
