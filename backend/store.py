"""SQLite store: encrypted per-user Delta credentials and a per-user audit log."""

from __future__ import annotations

import json
import os
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
    c = sqlite3.connect(path, timeout=10)
    c.execute("create table if not exists credentials(user_id text primary key, api_key_enc text not null, api_secret_enc text not null, env text not null, updated_at integer not null)")
    c.execute("create table if not exists events(id integer primary key autoincrement, user_id text not null, ts integer not null, action text not null, request text, ok integer not null, result text)")
    c.execute("create index if not exists ev_user on events(user_id, id)")
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
