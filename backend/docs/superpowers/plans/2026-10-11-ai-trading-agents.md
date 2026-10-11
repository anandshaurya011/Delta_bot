# AI-Powered TradingView Signal Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user wire a TradingView alert to a per-user webhook; a 3-agent LangGraph pipeline (coordinator, data_agent, executer) decides and autonomously executes trades on Delta Exchange, subject to deterministic Python-enforced guardrails and fill verification.

**Architecture:** New SQLite tables in `backend/store.py` hold per-user AI settings and a signal/decision log. A token-authed webhook endpoint does dedup/cooldown checks inside one SQLite transaction, then launches a background LangGraph pipeline. The pipeline's `data_agent` calls existing `delta_api.py` functions in-process; the `executer` runs a pure-Python guardrail check (no LLM in that decision), places the order, and polls for fill confirmation before marking a trade as executed. A new `/ai-trading` Next.js page lets the user configure everything.

**Tech Stack:** FastAPI, SQLite (existing `store.py` patterns), LangGraph, OpenAI Python SDK, existing `delta_api.py`, Next.js/TypeScript frontend (existing patterns in `frontend/lib/*.ts` + `frontend/app/(app)/*`).

**Spec:** `backend/docs/superpowers/specs/2026-10-11-ai-trading-agents-design.md`

## Global Constraints

- Local development only — no Pi deployment in this plan.
- One shared `OPENAI_API_KEY` (backend `.env`), not per-user.
- Guardrail allow/deny decisions are plain deterministic Python (`ai_guardrails.evaluate_guardrails`), never an LLM judgment call.
- A trade counts as "executed" (for cooldown + `max_trades_per_day`) only once confirmed `filled`/`partially_filled` — order acceptance alone is not enough.
- Duplicate-signal and single-flight checks run inside one SQLite transaction in the webhook request handler, before any background work starts.
- Cooldown is scoped to `(user_id, timeframe)`, not per-webhook-overall, and is only started by a confirmed executed trade.
- Dedup key: `sha256(symbol|timeframe|action|note)`, 120s window.
- All new SQLite tables live in `backend/store.py` (not a separate module) — matches user's explicit instruction to keep storage in one file.

## Review Focus

- **TradingView retries the same alert within seconds** — the second delivery must be `duplicate_skipped`, not a second trade. Covered in Task 3's dedup tests and Task 9's webhook endpoint test.
- **Order is accepted by Delta but never fills (e.g. limit far from market)** — must not start the cooldown or increment the daily trade counter. Covered in Task 7's `wait_for_fill` timeout test and Task 9's integration test.
- **User disables a guardrail entirely** — that check must be skipped, not treated as "pass with value 0". Covered in Task 4's disabled-guardrail tests.
- **Max daily loss is breached mid-session** — must flip `ai_settings.enabled` to false (kill switch), not just reject the one signal. Covered in Task 4 and Task 7.
- **Webhook token is regenerated while a signal is mid-flight** — the in-flight signal must still be identified by `user_id` already resolved at request start, not re-looked-up after regeneration. Covered in Task 2's regenerate test (old token immediately 404s) and noted in Task 9's endpoint implementation (resolve `user_id` once, before any guardrail/cooldown work).

---

## File Structure

- Modify `delta_api.py` — add `get_order_by_id`.
- Modify `backend/store.py` — add `ai_settings`, `ai_signals`, `ai_trade_count` tables and their access functions; adjust `_conn()` for `row_factory`/`isolation_level`.
- Create `backend/ai_guardrails.py` — pure guardrail evaluation, no I/O, no LLM.
- Create `backend/ai_pipeline.py` — LangGraph graph: state, coordinator node, data_agent node + tools, executer node (guardrails + order placement + fill verification), `run_pipeline()` entrypoint.
- Modify `backend/main.py` — new AI settings/signals endpoints + the TradingView webhook endpoint.
- Modify `backend/requirements.txt` — add `langgraph`, `openai`.
- Create `frontend/lib/aiTrading.ts` — types + pure validation/formatting helpers for the new page.
- Create `frontend/app/(app)/ai-trading/page.tsx` — the settings + decision-log UI.
- Tests: `backend/tests/test_ai_store.py`, `backend/tests/test_ai_guardrails.py`, `backend/tests/test_ai_pipeline.py`, `backend/tests/test_ai_webhook.py`, `frontend/lib/aiTrading.test.ts`.

---

### Task 1: `delta_api.py` — order-by-id lookup

**Files:**
- Modify: `delta_api.py` (add function near `get_open_orders`, ~line 685)
- Test: `backend/tests/test_ai_store.py` is created in Task 2; this task's behavior is exercised indirectly via Task 7's mocks, so verify this task with a direct interactive check instead of a new test file (no existing `delta_api.py` test module exists in this repo to extend).

**Interfaces:**
- Produces: `delta_api.get_order_by_id(order_id: int) -> dict` — `GET /v2/orders/{order_id}`, same auth/signing path as every other authed call in this file.

- [ ] **Step 1: Add the function**

```python
def get_order_by_id(order_id: int) -> dict:
    """Single order lookup, used to confirm fill status after placing an order."""
    return _request("GET", f"/v2/orders/{order_id}", auth=True)
```

Place it directly below `get_open_orders` (around line 690 of `delta_api.py`), so it sits with the other order-reading functions.

- [ ] **Step 2: Sanity-check it imports cleanly**

Run: `cd /home/anand-shaurya/Documents/personal/delta/backend && .venv/bin/python -c "import sys; sys.path.insert(0,'..'); import delta_api; print(delta_api.get_order_by_id)"`
Expected: prints `<function get_order_by_id at 0x...>` with no import errors.

- [ ] **Step 3: Commit**

```bash
cd /home/anand-shaurya/Documents/personal/delta
git add delta_api.py
git commit -m "feat(delta_api): add get_order_by_id for fill verification"
```

---

### Task 2: `store.py` — AI settings table

**Files:**
- Modify: `backend/store.py:27-34` (the `_conn()` function) and append new functions at the end of the file.
- Test: `backend/tests/test_ai_store.py` (new file — this task writes its first section)

**Interfaces:**
- Consumes: nothing new (uses existing `_conn()`, `crypto` is not needed here — settings aren't secret).
- Produces:
  - `store.get_or_create_ai_settings(user_id: str) -> dict` — dict with keys `user_id, webhook_token, enabled, cooldown_minutes, coordinator_model, data_model, executer_model, coordinator_prompt, data_prompt, executer_prompt, guardrails_json (already a dict, not a string), updated_at`.
  - `store.update_ai_settings(user_id: str, **fields) -> dict` — updates only the given keys (any of the above except `user_id`/`webhook_token`), returns the updated row as a dict (same shape as above).
  - `store.regenerate_webhook_token(user_id: str) -> str` — new token, persisted, returned.
  - `store.get_user_id_by_webhook_token(token: str) -> str | None` — only matches a row where `enabled = 1`; returns `None` otherwise.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_ai_store.py
import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("DELTA_DB_PATH", str(tmp_path / "test.db"))
    import store as store_module
    importlib.reload(store_module)
    return store_module


def test_get_or_create_ai_settings_creates_defaults(store):
    row = store.get_or_create_ai_settings("user_1")
    assert row["user_id"] == "user_1"
    assert row["enabled"] == 0
    assert row["cooldown_minutes"] == 30
    assert row["guardrails_json"] == {}
    assert row["webhook_token"]


def test_get_or_create_ai_settings_is_idempotent(store):
    first = store.get_or_create_ai_settings("user_1")
    second = store.get_or_create_ai_settings("user_1")
    assert first["webhook_token"] == second["webhook_token"]


def test_update_ai_settings(store):
    store.get_or_create_ai_settings("user_1")
    updated = store.update_ai_settings(
        "user_1", enabled=1, cooldown_minutes=15, guardrails_json={"max_trades_per_day": {"enabled": True, "value": 5}}
    )
    assert updated["enabled"] == 1
    assert updated["cooldown_minutes"] == 15
    assert updated["guardrails_json"]["max_trades_per_day"]["value"] == 5


def test_regenerate_webhook_token(store):
    original = store.get_or_create_ai_settings("user_1")
    new_token = store.regenerate_webhook_token("user_1")
    assert new_token != original["webhook_token"]
    assert store.get_user_id_by_webhook_token(original["webhook_token"]) is None
    assert store.get_user_id_by_webhook_token(new_token) == "user_1"


def test_get_user_id_by_webhook_token_requires_enabled(store):
    row = store.get_or_create_ai_settings("user_1")
    assert store.get_user_id_by_webhook_token(row["webhook_token"]) is None
    store.update_ai_settings("user_1", enabled=1)
    assert store.get_user_id_by_webhook_token(row["webhook_token"]) == "user_1"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /home/anand-shaurya/Documents/personal/delta/backend && .venv/bin/python -m pytest tests/test_ai_store.py -v`
Expected: FAIL — `AttributeError: module 'store' has no attribute 'get_or_create_ai_settings'`.

- [ ] **Step 3: Update `_conn()` and add the new table + functions**

In `backend/store.py`, change `_conn()`:

```python
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
    return c
```

`isolation_level=None` puts the connection in autocommit mode so Task 3's explicit `begin immediate` transaction works correctly; it does not change behavior for existing code, which already commits explicitly via `with closing(_conn()) as c, c:`. `row_factory = sqlite3.Row` is additive — existing index-based access (`row[0]`) still works, and new code can use `row["col"]`.

Append to `backend/store.py`:

```python
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
```

Add `import secrets` to the top of `backend/store.py` alongside the existing imports.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /home/anand-shaurya/Documents/personal/delta/backend && .venv/bin/python -m pytest tests/test_ai_store.py -v`
Expected: 5 passed.

- [ ] **Step 5: Run the full existing backend suite to confirm no regression from the `_conn()` change**

Run: `cd /home/anand-shaurya/Documents/personal/delta/backend && .venv/bin/python -m pytest -v`
Expected: all existing tests + the 5 new ones pass.

- [ ] **Step 6: Commit**

```bash
cd /home/anand-shaurya/Documents/personal/delta
git add backend/store.py backend/tests/test_ai_store.py
git commit -m "feat(store): add ai_settings table for per-user AI trading config"
```

---

### Task 3: `store.py` — signal log, dedup/cooldown, trade count

**Files:**
- Modify: `backend/store.py` (append)
- Test: `backend/tests/test_ai_store.py` (append)

**Interfaces:**
- Consumes: `_conn()` from Task 2 (already autocommit + `Row` factory).
- Produces:
  - `store.record_incoming_signal(user_id: str, symbol: str, timeframe: str, action: str, note: str | None, cooldown_minutes: int) -> dict` — returns `{"id": int, "status": "processing"|"duplicate_skipped"|"cooldown_skipped"}`. This is the single entry point the webhook endpoint (Task 9) calls; all dedup/single-flight/cooldown logic lives here.
  - `store.update_signal_result(signal_id: int, status: str, result: dict) -> None` — `status` is `"done"` or `"error"`.
  - `store.list_signals(user_id: str, limit: int = 50) -> list[dict]` — newest first, `result_json` already parsed back into a dict (or `None`).

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_ai_store.py`:

```python
def test_record_incoming_signal_first_time_is_processing(store):
    result = store.record_incoming_signal("user_1", "BTCUSD", "15m", "buy", None, cooldown_minutes=30)
    assert result["status"] == "processing"
    assert result["id"] > 0


def test_record_incoming_signal_dedups_identical_payload(store):
    first = store.record_incoming_signal("user_1", "BTCUSD", "15m", "buy", "note", cooldown_minutes=30)
    store.update_signal_result(first["id"], "done", {"execution_result": {"executed": False}})
    second = store.record_incoming_signal("user_1", "BTCUSD", "15m", "buy", "note", cooldown_minutes=30)
    assert second["status"] == "duplicate_skipped"


def test_record_incoming_signal_single_flight_blocks_same_timeframe(store):
    first = store.record_incoming_signal("user_1", "BTCUSD", "15m", "buy", "a", cooldown_minutes=30)
    assert first["status"] == "processing"
    second = store.record_incoming_signal("user_1", "ETHUSD", "15m", "sell", "b", cooldown_minutes=30)
    assert second["status"] == "duplicate_skipped"


def test_record_incoming_signal_different_timeframe_not_blocked(store):
    store.record_incoming_signal("user_1", "BTCUSD", "15m", "buy", "a", cooldown_minutes=30)
    other = store.record_incoming_signal("user_1", "BTCUSD", "1h", "buy", "a", cooldown_minutes=30)
    assert other["status"] == "processing"


def test_cooldown_only_starts_after_executed_trade(store):
    held = store.record_incoming_signal("user_1", "BTCUSD", "15m", "buy", "x1", cooldown_minutes=30)
    store.update_signal_result(held["id"], "done", {"execution_result": {"executed": False}})
    not_cooled = store.record_incoming_signal("user_1", "BTCUSD", "15m", "buy", "x2", cooldown_minutes=30)
    assert not_cooled["status"] == "processing"
    store.update_signal_result(not_cooled["id"], "done", {"execution_result": {"executed": True}})
    cooled = store.record_incoming_signal("user_1", "BTCUSD", "15m", "buy", "x3", cooldown_minutes=30)
    assert cooled["status"] == "cooldown_skipped"


def test_list_signals_returns_newest_first_with_parsed_result(store):
    a = store.record_incoming_signal("user_1", "BTCUSD", "15m", "buy", "a", cooldown_minutes=30)
    store.update_signal_result(a["id"], "done", {"plan": {"action": "hold"}})
    rows = store.list_signals("user_1", limit=10)
    assert rows[0]["id"] == a["id"]
    assert rows[0]["result_json"] == {"plan": {"action": "hold"}}


def test_trade_count_increments_and_reads_back(store):
    assert store.get_trade_count_today("user_1") == 0
    store.increment_trade_count("user_1")
    store.increment_trade_count("user_1")
    assert store.get_trade_count_today("user_1") == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /home/anand-shaurya/Documents/personal/delta/backend && .venv/bin/python -m pytest tests/test_ai_store.py -v`
Expected: the new tests FAIL with `AttributeError` (functions don't exist yet); the Task 2 tests still pass.

- [ ] **Step 3: Implement**

Append to `backend/store.py`:

```python
AI_DEDUP_WINDOW_SECONDS = 120


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
                "select id from ai_signals where user_id=? and timeframe=? and status='processing' limit 1",
                (user_id, timeframe),
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
```

Add `import hashlib` to the top of `backend/store.py`.

Note on `test_record_incoming_signal_single_flight_blocks_same_timeframe`: the single-flight lock is scoped to `(user_id, timeframe)`, so that test uses the *same* timeframe (`"15m"`) for both calls deliberately — a different symbol doesn't bypass the lock, only a different timeframe does (see the next test).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /home/anand-shaurya/Documents/personal/delta/backend && .venv/bin/python -m pytest tests/test_ai_store.py -v`
Expected: all passed (12 total across Task 2 + 3).

- [ ] **Step 5: Commit**

```bash
cd /home/anand-shaurya/Documents/personal/delta
git add backend/store.py backend/tests/test_ai_store.py
git commit -m "feat(store): add ai_signals log with dedup, single-flight, and cooldown"
```

---

### Task 4: `backend/ai_guardrails.py` — deterministic guardrail evaluation

**Files:**
- Create: `backend/ai_guardrails.py`
- Test: `backend/tests/test_ai_guardrails.py`

**Interfaces:**
- Produces:
  - `ai_guardrails.GuardrailResult` — dataclass `{allowed: bool, reason: str | None, disable_webhook: bool}`.
  - `ai_guardrails.evaluate_guardrails(order_request: dict, guardrails: dict, context: dict) -> GuardrailResult`.
    - `order_request`: `{"symbol": str, "side": "buy"|"sell", "size": int}` (size is always positive; direction is in `side`).
    - `guardrails`: the `guardrails_json` dict from `ai_settings` (shape from the spec §3).
    - `context`: `{"existing_position_size": int, "trades_today": int, "daily_pnl": float}` — `existing_position_size` is signed (positive = long, negative = short); `daily_pnl` is signed (negative = loss).

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_ai_guardrails.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_guardrails import evaluate_guardrails

BASE_CONTEXT = {"existing_position_size": 0, "trades_today": 0, "daily_pnl": 0.0}


def test_all_guardrails_disabled_always_allows():
    order = {"symbol": "DOGEUSD", "side": "buy", "size": 10_000}
    guardrails = {
        "max_position_size": {"enabled": False, "value": 1},
        "symbol_allowlist": {"enabled": False, "value": []},
        "max_trades_per_day": {"enabled": False, "value": 0},
        "max_daily_loss": {"enabled": False, "value": 0},
    }
    result = evaluate_guardrails(order, guardrails, BASE_CONTEXT)
    assert result.allowed is True


def test_symbol_allowlist_rejects_unlisted_symbol():
    order = {"symbol": "DOGEUSD", "side": "buy", "size": 1}
    guardrails = {"symbol_allowlist": {"enabled": True, "value": ["BTCUSD"]}}
    result = evaluate_guardrails(order, guardrails, BASE_CONTEXT)
    assert result.allowed is False
    assert result.reason == "guardrail:symbol_allowlist"


def test_max_position_size_rejects_when_resulting_size_too_large():
    order = {"symbol": "BTCUSD", "side": "buy", "size": 40}
    guardrails = {"max_position_size": {"enabled": True, "value": 50}}
    context = {**BASE_CONTEXT, "existing_position_size": 20}
    result = evaluate_guardrails(order, guardrails, context)
    assert result.allowed is False
    assert result.reason == "guardrail:max_position_size"


def test_max_position_size_allows_within_cap():
    order = {"symbol": "BTCUSD", "side": "buy", "size": 10}
    guardrails = {"max_position_size": {"enabled": True, "value": 50}}
    context = {**BASE_CONTEXT, "existing_position_size": 20}
    result = evaluate_guardrails(order, guardrails, context)
    assert result.allowed is True


def test_max_trades_per_day_rejects_at_cap():
    order = {"symbol": "BTCUSD", "side": "buy", "size": 1}
    guardrails = {"max_trades_per_day": {"enabled": True, "value": 3}}
    context = {**BASE_CONTEXT, "trades_today": 3}
    result = evaluate_guardrails(order, guardrails, context)
    assert result.allowed is False
    assert result.reason == "guardrail:max_trades_per_day"


def test_max_daily_loss_rejects_and_requests_webhook_disable():
    order = {"symbol": "BTCUSD", "side": "sell", "size": 1}
    guardrails = {"max_daily_loss": {"enabled": True, "value": 200}}
    context = {**BASE_CONTEXT, "daily_pnl": -250.0}
    result = evaluate_guardrails(order, guardrails, context)
    assert result.allowed is False
    assert result.reason == "guardrail:max_daily_loss"
    assert result.disable_webhook is True


def test_guardrail_missing_from_config_is_treated_as_disabled():
    order = {"symbol": "BTCUSD", "side": "buy", "size": 9999}
    result = evaluate_guardrails(order, {}, BASE_CONTEXT)
    assert result.allowed is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /home/anand-shaurya/Documents/personal/delta/backend && .venv/bin/python -m pytest tests/test_ai_guardrails.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ai_guardrails'`.

- [ ] **Step 3: Implement**

```python
# backend/ai_guardrails.py
"""Deterministic, Python-only guardrail checks for the AI executer.

No LLM is ever consulted for this decision — the coordinator/executer agents
produce a proposed order; this module is the sole authority on whether it is
allowed to execute.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GuardrailResult:
    allowed: bool
    reason: str | None = None
    disable_webhook: bool = False


def evaluate_guardrails(order_request: dict[str, Any], guardrails: dict[str, Any], context: dict[str, Any]) -> GuardrailResult:
    allowlist = guardrails.get("symbol_allowlist", {})
    if allowlist.get("enabled") and order_request["symbol"] not in allowlist.get("value", []):
        return GuardrailResult(False, "guardrail:symbol_allowlist")

    max_size = guardrails.get("max_position_size", {})
    if max_size.get("enabled"):
        direction = 1 if order_request["side"] == "buy" else -1
        resulting = context.get("existing_position_size", 0) + direction * order_request["size"]
        if abs(resulting) > max_size.get("value", 0):
            return GuardrailResult(False, "guardrail:max_position_size")

    max_trades = guardrails.get("max_trades_per_day", {})
    if max_trades.get("enabled") and context.get("trades_today", 0) >= max_trades.get("value", 0):
        return GuardrailResult(False, "guardrail:max_trades_per_day")

    max_loss = guardrails.get("max_daily_loss", {})
    if max_loss.get("enabled") and context.get("daily_pnl", 0.0) <= -abs(max_loss.get("value", 0)):
        return GuardrailResult(False, "guardrail:max_daily_loss", disable_webhook=True)

    return GuardrailResult(True)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /home/anand-shaurya/Documents/personal/delta/backend && .venv/bin/python -m pytest tests/test_ai_guardrails.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
cd /home/anand-shaurya/Documents/personal/delta
git add backend/ai_guardrails.py backend/tests/test_ai_guardrails.py
git commit -m "feat: add deterministic, LLM-free guardrail evaluation"
```

---

### Task 5: `backend/ai_pipeline.py` — pipeline state + coordinator node skeleton

**Files:**
- Modify: `backend/requirements.txt` (add `langgraph`, `openai`)
- Create: `backend/ai_pipeline.py`
- Test: `backend/tests/test_ai_pipeline.py`

**Interfaces:**
- Consumes: nothing from earlier tasks yet (this task is pure structure + the coordinator node, mocked LLM calls in tests).
- Produces:
  - `ai_pipeline.PipelineState` — `TypedDict` with keys `user_id: str`, `signal: dict`, `settings: dict`, `gathered_data: dict`, `plan: dict | None`, `order_request: dict | None`, `execution_result: dict | None`.
  - `ai_pipeline.call_llm(model: str, system_prompt: str, user_content: str, tools: list[dict] | None = None) -> Any` — thin wrapper around `openai.OpenAI().chat.completions.create(...)`, so every node (and every test) mocks this one function instead of the OpenAI client directly.
  - `ai_pipeline.coordinator_node(state: PipelineState) -> PipelineState` — calls `call_llm` with `settings["coordinator_model"]` and a system prompt built from a fixed base + `settings["coordinator_prompt"]`; parses the model's JSON response into `state["plan"] = {"action": ..., "side": ..., "size_hint": ..., "reasoning": ...}`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_ai_pipeline.py
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ai_pipeline


def _state(**overrides):
    base = {
        "user_id": "user_1",
        "signal": {"symbol": "BTCUSD", "timeframe": "15m", "action": "buy", "note": None},
        "settings": {
            "coordinator_model": "gpt-4.1",
            "data_model": "gpt-4.1-mini",
            "executer_model": "gpt-4.1-mini",
            "coordinator_prompt": "",
            "data_prompt": "",
            "executer_prompt": "",
            "guardrails_json": {},
        },
        "gathered_data": {},
        "plan": None,
        "order_request": None,
        "execution_result": None,
    }
    base.update(overrides)
    return base


def test_coordinator_node_parses_plan_from_llm_response():
    fake_response = json.dumps({"action": "open", "side": "long", "size_hint": 5, "reasoning": "signal says buy"})
    with patch("ai_pipeline.call_llm", return_value=fake_response) as mock_call:
        state = ai_pipeline.coordinator_node(_state())
    assert state["plan"] == {"action": "open", "side": "long", "size_hint": 5, "reasoning": "signal says buy"}
    mock_call.assert_called_once()
    _, kwargs = mock_call.call_args
    assert kwargs["model"] == "gpt-4.1"


def test_coordinator_node_includes_custom_instructions_in_prompt():
    fake_response = json.dumps({"action": "hold", "side": "none", "size_hint": None, "reasoning": "x"})
    state = _state()
    state["settings"]["coordinator_prompt"] = "Never trade on Fridays."
    with patch("ai_pipeline.call_llm", return_value=fake_response) as mock_call:
        ai_pipeline.coordinator_node(state)
    _, kwargs = mock_call.call_args
    assert "Never trade on Fridays." in kwargs["system_prompt"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /home/anand-shaurya/Documents/personal/delta/backend && .venv/bin/python -m pytest tests/test_ai_pipeline.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ai_pipeline'`.

- [ ] **Step 3: Add dependencies and implement**

Add to `backend/requirements.txt`:

```
langgraph
openai
```

```python
# backend/ai_pipeline.py
"""LangGraph pipeline: coordinator -> data_agent -> executer.

Every node calls `call_llm`, a single thin wrapper, so tests mock one
function instead of the OpenAI client. Guardrail enforcement (Task 7) is
plain Python and never goes through `call_llm`.
"""

from __future__ import annotations

import json
from typing import Any, TypedDict

from openai import OpenAI

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI()
    return _client


class PipelineState(TypedDict, total=False):
    user_id: str
    signal: dict[str, Any]
    settings: dict[str, Any]
    gathered_data: dict[str, Any]
    plan: dict[str, Any] | None
    order_request: dict[str, Any] | None
    execution_result: dict[str, Any] | None


def call_llm(model: str, system_prompt: str, user_content: str, tools: list[dict[str, Any]] | None = None) -> str:
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_content}],
        "response_format": {"type": "json_object"},
    }
    if tools:
        kwargs["tools"] = tools
    response = _get_client().chat.completions.create(**kwargs)
    return response.choices[0].message.content


_COORDINATOR_BASE_PROMPT = (
    "You are the coordinator agent for an automated trading system. You receive a "
    "TradingView signal and the data the data_agent gathered. Decide one action: "
    "open, add, reduce, flip, hold, or close. Respond ONLY with JSON: "
    '{"action": "...", "side": "long"|"short"|"none", "size_hint": number|null, "reasoning": "..."}'
)


def coordinator_node(state: PipelineState) -> PipelineState:
    settings = state["settings"]
    system_prompt = _COORDINATOR_BASE_PROMPT
    if settings.get("coordinator_prompt"):
        system_prompt += "\n\nUser instructions: " + settings["coordinator_prompt"]
    user_content = json.dumps({"signal": state["signal"], "gathered_data": state.get("gathered_data", {})})
    raw = call_llm(model=settings["coordinator_model"], system_prompt=system_prompt, user_content=user_content)
    state["plan"] = json.loads(raw)
    return state
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /home/anand-shaurya/Documents/personal/delta/backend && .venv/bin/python -m pytest tests/test_ai_pipeline.py -v`
Expected: 2 passed.

- [ ] **Step 5: Install the new dependencies in the venv**

Run: `cd /home/anand-shaurya/Documents/personal/delta/backend && .venv/bin/pip install langgraph openai`
Expected: both install cleanly with no conflicts against existing `requirements.txt` packages.

- [ ] **Step 6: Commit**

```bash
cd /home/anand-shaurya/Documents/personal/delta
git add backend/requirements.txt backend/ai_pipeline.py backend/tests/test_ai_pipeline.py
git commit -m "feat: add LangGraph pipeline skeleton with coordinator node"
```

---

### Task 6: `backend/ai_pipeline.py` — data_agent node + in-process delta_api tools

**Files:**
- Modify: `backend/ai_pipeline.py` (append)
- Test: `backend/tests/test_ai_pipeline.py` (append)

**Interfaces:**
- Consumes: `PipelineState`, `call_llm` from Task 5; `delta_api` functions `get_position`, `get_current_price`, `analyze_symbol`, `get_balances` (already exist in `delta_api.py`).
- Produces:
  - `ai_pipeline.bind_user_credentials(user_id: str) -> None` — looks up `store.get_credentials(user_id)` and calls `delta_api.use_credentials(...)`, same pattern as `auth.bind_user`. Must be called once at the start of `run_pipeline` (Task 7) since the background task runs outside the request's contextvar scope.
  - `ai_pipeline.DATA_TOOLS` — list of tool-call definitions (`get_position`, `get_price`, `get_analysis`, `get_balances`) exposed to the data_agent's `call_llm` invocation.
  - `ai_pipeline.data_agent_node(state: PipelineState) -> PipelineState` — calls `call_llm` with `settings["data_model"]`, executes any requested tool calls against `delta_api` directly (in-process, no HTTP), and sets `state["gathered_data"]` to the combined tool results.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_ai_pipeline.py`:

```python
from unittest.mock import MagicMock


def test_bind_user_credentials_uses_stored_creds():
    fake_creds = MagicMock(api_key="k", api_secret="s", env="india_testnet")
    with patch("ai_pipeline.store.get_credentials", return_value=fake_creds) as mock_get, \
         patch("ai_pipeline.delta.use_credentials") as mock_use:
        ai_pipeline.bind_user_credentials("user_1")
    mock_get.assert_called_once_with("user_1")
    mock_use.assert_called_once_with("k", "s", "india_testnet")


def test_data_agent_node_runs_requested_tool_calls_and_fills_gathered_data():
    tool_call_response = json.dumps({"tool_calls": [{"name": "get_position", "args": {"symbol": "BTCUSD"}}]})
    with patch("ai_pipeline.call_llm", return_value=tool_call_response), \
         patch("ai_pipeline.delta.get_position", return_value={"size": 5, "entry_price": "100"}) as mock_pos:
        state = ai_pipeline.data_agent_node(_state())
    mock_pos.assert_called_once_with("BTCUSD")
    assert state["gathered_data"]["get_position"] == {"size": 5, "entry_price": "100"}


def test_data_agent_node_with_no_tool_calls_leaves_gathered_data_empty():
    with patch("ai_pipeline.call_llm", return_value=json.dumps({"tool_calls": []})):
        state = ai_pipeline.data_agent_node(_state())
    assert state["gathered_data"] == {}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /home/anand-shaurya/Documents/personal/delta/backend && .venv/bin/python -m pytest tests/test_ai_pipeline.py -v`
Expected: the 3 new tests FAIL (`AttributeError`); Task 5's 2 tests still pass.

- [ ] **Step 3: Implement**

Append to `backend/ai_pipeline.py`:

```python
import delta_api as delta
import store

DATA_TOOLS = [
    {"name": "get_position", "args": {"symbol": "str"}},
    {"name": "get_price", "args": {"symbol": "str"}},
    {"name": "get_analysis", "args": {"symbol": "str", "resolution": "str"}},
    {"name": "get_balances", "args": {}},
]

_TOOL_IMPL = {
    "get_position": lambda args: delta.get_position(args["symbol"]),
    "get_price": lambda args: delta.get_current_price(args["symbol"]),
    "get_analysis": lambda args: delta.analyze_symbol(args["symbol"], resolutions=[args.get("resolution", "1h")]),
    "get_balances": lambda args: delta.get_balances(),
}


def bind_user_credentials(user_id: str) -> None:
    creds = store.get_credentials(user_id)
    if creds:
        delta.use_credentials(creds.api_key, creds.api_secret, creds.env)


_DATA_AGENT_BASE_PROMPT = (
    "You are the data_agent for an automated trading system. Given a signal and the "
    "available tools, decide which tools to call to gather the context the coordinator "
    f"needs. Available tools: {json.dumps(DATA_TOOLS)}. Respond ONLY with JSON: "
    '{"tool_calls": [{"name": "...", "args": {...}}, ...]}'
)


def data_agent_node(state: PipelineState) -> PipelineState:
    settings = state["settings"]
    system_prompt = _DATA_AGENT_BASE_PROMPT
    if settings.get("data_prompt"):
        system_prompt += "\n\nUser instructions: " + settings["data_prompt"]
    user_content = json.dumps({"signal": state["signal"]})
    raw = call_llm(model=settings["data_model"], system_prompt=system_prompt, user_content=user_content)
    requested = json.loads(raw).get("tool_calls", [])
    gathered: dict[str, Any] = {}
    for call in requested:
        impl = _TOOL_IMPL.get(call["name"])
        if impl:
            gathered[call["name"]] = impl(call.get("args", {}))
    state["gathered_data"] = gathered
    return state
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /home/anand-shaurya/Documents/personal/delta/backend && .venv/bin/python -m pytest tests/test_ai_pipeline.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
cd /home/anand-shaurya/Documents/personal/delta
git add backend/ai_pipeline.py backend/tests/test_ai_pipeline.py
git commit -m "feat: add data_agent node with in-process delta_api tool calls"
```

---

### Task 7: `backend/ai_pipeline.py` — executer node, guardrails, fill verification

**Files:**
- Modify: `backend/ai_pipeline.py` (append)
- Test: `backend/tests/test_ai_pipeline.py` (append)

**Interfaces:**
- Consumes: `PipelineState`, `call_llm`, `bind_user_credentials` from Tasks 5-6; `ai_guardrails.evaluate_guardrails` from Task 4; `delta_api.place_order`, `delta_api.get_order_by_id`, `delta_api.get_position`, `delta_api.get_total_unrealized_pnl`; `store.get_trade_count_today`, `store.increment_trade_count`.
- Produces:
  - `ai_pipeline.wait_for_fill(order_id: int, timeout_s: float = 10.0, interval_s: float = 1.0) -> dict` — returns `{"outcome": "filled"|"partially_filled"|"unfilled_timeout"|"exchange_rejected", "order": dict}`.
  - `ai_pipeline.executer_node(state: PipelineState) -> PipelineState` — sets `state["execution_result"]` to one of:
    - `{"executed": False, "reason": "guardrail:<name>"}` (guardrail rejection — plan's action was `hold`/`close`-with-nothing-to-close also short-circuits here with `reason: "no_action"`, before any guardrail check)
    - `{"executed": False, "reason": "unfilled_timeout"|"exchange_rejected", "order": {...}}`
    - `{"executed": True, "order": {...}, "fill_status": "filled"|"partially_filled"}`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_ai_pipeline.py`:

```python
def test_wait_for_fill_returns_filled_when_unfilled_size_zero():
    with patch("ai_pipeline.delta.get_order_by_id", return_value={"id": 1, "state": "closed", "unfilled_size": 0}):
        result = ai_pipeline.wait_for_fill(1, timeout_s=1, interval_s=0.01)
    assert result["outcome"] == "filled"


def test_wait_for_fill_returns_exchange_rejected():
    with patch("ai_pipeline.delta.get_order_by_id", return_value={"id": 1, "state": "cancelled", "unfilled_size": 5}):
        result = ai_pipeline.wait_for_fill(1, timeout_s=1, interval_s=0.01)
    assert result["outcome"] == "exchange_rejected"


def test_wait_for_fill_times_out_when_still_open():
    with patch("ai_pipeline.delta.get_order_by_id", return_value={"id": 1, "state": "open", "unfilled_size": 5}):
        result = ai_pipeline.wait_for_fill(1, timeout_s=0.05, interval_s=0.01)
    assert result["outcome"] == "unfilled_timeout"


def test_executer_node_holds_without_calling_llm_or_placing_order():
    state = _state(plan={"action": "hold", "side": "none", "size_hint": None, "reasoning": "nothing to do"})
    with patch("ai_pipeline.delta.place_order") as mock_place:
        state = ai_pipeline.executer_node(state)
    mock_place.assert_not_called()
    assert state["execution_result"] == {"executed": False, "reason": "no_action"}


def test_executer_node_rejects_via_guardrail_without_placing_order():
    state = _state(plan={"action": "open", "side": "long", "size_hint": 100, "reasoning": "go long"})
    state["settings"]["guardrails_json"] = {"max_position_size": {"enabled": True, "value": 10}}
    order_response = json.dumps({"symbol": "BTCUSD", "side": "buy", "size": 100})
    with patch("ai_pipeline.call_llm", return_value=order_response), \
         patch("ai_pipeline.delta.get_position", return_value=None), \
         patch("ai_pipeline.delta.get_total_unrealized_pnl", return_value=0.0), \
         patch("ai_pipeline.store.get_trade_count_today", return_value=0), \
         patch("ai_pipeline.delta.place_order") as mock_place:
        state = ai_pipeline.executer_node(state)
    mock_place.assert_not_called()
    assert state["execution_result"] == {"executed": False, "reason": "guardrail:max_position_size"}


def test_executer_node_places_order_and_confirms_fill():
    state = _state(plan={"action": "open", "side": "long", "size_hint": 5, "reasoning": "go long"})
    order_response = json.dumps({"symbol": "BTCUSD", "side": "buy", "size": 5})
    placed_order = {"id": 42}
    filled_order = {"id": 42, "state": "closed", "unfilled_size": 0}
    with patch("ai_pipeline.call_llm", return_value=order_response), \
         patch("ai_pipeline.delta.get_position", return_value=None), \
         patch("ai_pipeline.delta.get_total_unrealized_pnl", return_value=0.0), \
         patch("ai_pipeline.store.get_trade_count_today", return_value=0), \
         patch("ai_pipeline.store.increment_trade_count") as mock_inc, \
         patch("ai_pipeline.delta.place_order", return_value=placed_order), \
         patch("ai_pipeline.delta.get_order_by_id", return_value=filled_order):
        state = ai_pipeline.executer_node(state)
    assert state["execution_result"]["executed"] is True
    assert state["execution_result"]["fill_status"] == "filled"
    mock_inc.assert_called_once()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /home/anand-shaurya/Documents/personal/delta/backend && .venv/bin/python -m pytest tests/test_ai_pipeline.py -v`
Expected: the 6 new tests FAIL (`AttributeError`); the earlier 5 still pass.

- [ ] **Step 3: Implement**

Append to `backend/ai_pipeline.py`:

```python
import time as _time

from ai_guardrails import evaluate_guardrails

_EXECUTER_BASE_PROMPT = (
    "You are the executer agent. Given the coordinator's plan, produce the concrete order "
    "request. You do not decide whether the trade is allowed — that is checked separately. "
    'Respond ONLY with JSON: {"symbol": "...", "side": "buy"|"sell", "size": integer}'
)


def wait_for_fill(order_id: int, timeout_s: float = 10.0, interval_s: float = 1.0) -> dict[str, Any]:
    deadline = _time.time() + timeout_s
    while True:
        order = delta.get_order_by_id(order_id)
        unfilled = order.get("unfilled_size", 0)
        state = order.get("state")
        if unfilled == 0:
            return {"outcome": "filled", "order": order}
        if state == "closed":
            return {"outcome": "partially_filled", "order": order}
        if state in ("cancelled", "rejected"):
            return {"outcome": "exchange_rejected", "order": order}
        if _time.time() >= deadline:
            return {"outcome": "unfilled_timeout", "order": order}
        _time.sleep(interval_s)


def executer_node(state: PipelineState) -> PipelineState:
    plan = state["plan"] or {}
    if plan.get("action") in (None, "hold") or plan.get("side") == "none":
        state["execution_result"] = {"executed": False, "reason": "no_action"}
        return state

    settings = state["settings"]
    system_prompt = _EXECUTER_BASE_PROMPT
    if settings.get("executer_prompt"):
        system_prompt += "\n\nUser instructions: " + settings["executer_prompt"]
    user_content = json.dumps({"plan": plan, "signal": state["signal"]})
    raw = call_llm(model=settings["executer_model"], system_prompt=system_prompt, user_content=user_content)
    order_request = json.loads(raw)
    state["order_request"] = order_request

    symbol = order_request["symbol"]
    position = delta.get_position(symbol)
    existing_size = position["size"] if position else 0
    context = {
        "existing_position_size": existing_size,
        "trades_today": store.get_trade_count_today(state["user_id"]),
        "daily_pnl": delta.get_total_unrealized_pnl(),
    }
    guardrail_result = evaluate_guardrails(order_request, settings.get("guardrails_json", {}), context)
    if not guardrail_result.allowed:
        state["execution_result"] = {"executed": False, "reason": guardrail_result.reason}
        return state

    order = delta.place_order(symbol, order_request["side"], order_request["size"])
    fill = wait_for_fill(order["id"])
    if fill["outcome"] in ("filled", "partially_filled"):
        store.increment_trade_count(state["user_id"])
        state["execution_result"] = {"executed": True, "order": fill["order"], "fill_status": fill["outcome"]}
    else:
        state["execution_result"] = {"executed": False, "reason": fill["outcome"], "order": fill["order"]}
    return state
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /home/anand-shaurya/Documents/personal/delta/backend && .venv/bin/python -m pytest tests/test_ai_pipeline.py -v`
Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
cd /home/anand-shaurya/Documents/personal/delta
git add backend/ai_pipeline.py backend/tests/test_ai_pipeline.py
git commit -m "feat: add executer node with python-enforced guardrails and fill verification"
```

---

### Task 8: `backend/ai_pipeline.py` — LangGraph wiring + `run_pipeline` entrypoint

**Files:**
- Modify: `backend/ai_pipeline.py` (append)
- Test: `backend/tests/test_ai_pipeline.py` (append)

**Interfaces:**
- Consumes: `coordinator_node`, `data_agent_node`, `executer_node`, `bind_user_credentials`, `PipelineState` (all from Tasks 5-7).
- Produces: `ai_pipeline.run_pipeline(user_id: str, signal: dict, settings: dict) -> dict` — binds credentials, runs the graph (`data_agent -> coordinator -> executer`, matching the spec's "coordinator can request data before planning" by always gathering data first in this phase — see note below), returns the final `result_json` shape: `{"gathered_data": ..., "plan": ..., "order_request": ..., "execution_result": ...}`.

Note: the spec describes the coordinator optionally calling a `request_data` tool. For this plan, `run_pipeline` always runs `data_agent` before `coordinator` (data_agent decides on its own, via its tool-call response, what's worth gathering) rather than giving the coordinator a dynamic back-and-forth loop — this keeps the graph a simple fixed sequence while still satisfying "data_agent gathers whatever the coordinator will need," and is easy to extend into a loop later without changing any node's signature.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_ai_pipeline.py`:

```python
def test_run_pipeline_wires_all_three_nodes_in_order():
    data_response = json.dumps({"tool_calls": []})
    plan_response = json.dumps({"action": "hold", "side": "none", "size_hint": None, "reasoning": "no edge"})
    with patch("ai_pipeline.bind_user_credentials") as mock_bind, \
         patch("ai_pipeline.call_llm", side_effect=[data_response, plan_response]):
        result = ai_pipeline.run_pipeline(
            "user_1",
            {"symbol": "BTCUSD", "timeframe": "15m", "action": "buy", "note": None},
            _state()["settings"],
        )
    mock_bind.assert_called_once_with("user_1")
    assert result["plan"]["action"] == "hold"
    assert result["execution_result"] == {"executed": False, "reason": "no_action"}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /home/anand-shaurya/Documents/personal/delta/backend && .venv/bin/python -m pytest tests/test_ai_pipeline.py -v`
Expected: the new test FAILS (`AttributeError: module 'ai_pipeline' has no attribute 'run_pipeline'`).

- [ ] **Step 3: Implement**

Append to `backend/ai_pipeline.py`:

```python
from langgraph.graph import StateGraph, END

_graph = StateGraph(PipelineState)
_graph.add_node("data_agent", data_agent_node)
_graph.add_node("coordinator", coordinator_node)
_graph.add_node("executer", executer_node)
_graph.set_entry_point("data_agent")
_graph.add_edge("data_agent", "coordinator")
_graph.add_edge("coordinator", "executer")
_graph.add_edge("executer", END)
_compiled_graph = _graph.compile()


def run_pipeline(user_id: str, signal: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    bind_user_credentials(user_id)
    initial_state: PipelineState = {
        "user_id": user_id,
        "signal": signal,
        "settings": settings,
        "gathered_data": {},
        "plan": None,
        "order_request": None,
        "execution_result": None,
    }
    final_state = _compiled_graph.invoke(initial_state)
    return {
        "gathered_data": final_state["gathered_data"],
        "plan": final_state["plan"],
        "order_request": final_state["order_request"],
        "execution_result": final_state["execution_result"],
    }
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd /home/anand-shaurya/Documents/personal/delta/backend && .venv/bin/python -m pytest tests/test_ai_pipeline.py -v`
Expected: 12 passed.

- [ ] **Step 5: Commit**

```bash
cd /home/anand-shaurya/Documents/personal/delta
git add backend/ai_pipeline.py backend/tests/test_ai_pipeline.py
git commit -m "feat: wire data_agent -> coordinator -> executer into a LangGraph pipeline"
```

---

### Task 9: `backend/main.py` — AI settings, signals, and webhook endpoints

**Files:**
- Modify: `backend/main.py` (append new section near the end, after the existing `/settings/delta` endpoints around line 416)
- Test: `backend/tests/test_ai_webhook.py` (new file — follows the existing pattern in `backend/tests/test_api.py`/`conftest.py` for a `TestClient`)

**Interfaces:**
- Consumes: everything from Tasks 2-8 (`store.*`, `ai_pipeline.run_pipeline`).
- Produces the HTTP surface from the spec §7:
  - `GET /ai/settings`, `PUT /ai/settings`, `POST /ai/webhook/regenerate`, `GET /ai/signals` — all Clerk-authed (sit behind the app's existing `Depends(auth.bind_user)`).
  - `POST /webhook/tv/{token}` — **not** Clerk-authed; resolve the user from the token.

- [ ] **Step 1: Check the existing test fixtures**

Run: `cd /home/anand-shaurya/Documents/personal/delta/backend && cat tests/conftest.py`

Reuse whatever `TestClient`/auth-bypass fixture already exists there (the existing `test_api.py` already has to work around the Clerk `Depends(auth.bind_user)` dependency for every authed endpoint — follow that exact pattern for the new authed endpoints rather than inventing a new one).

- [ ] **Step 2: Write the failing tests**

```python
# backend/tests/test_ai_webhook.py
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DELTA_DB_PATH", str(tmp_path / "test.db"))
    import importlib
    import store as store_module
    importlib.reload(store_module)
    import main as main_module
    importlib.reload(main_module)
    main_module.app.dependency_overrides[main_module.auth.bind_user] = lambda: "user_1"
    return TestClient(main_module.app)


def test_get_ai_settings_creates_default_row(client):
    resp = client.get("/ai/settings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False or body["enabled"] == 0
    assert body["webhook_token"]


def test_put_ai_settings_updates_fields(client):
    client.get("/ai/settings")
    resp = client.put("/ai/settings", json={"enabled": True, "cooldown_minutes": 10})
    assert resp.status_code == 200
    assert resp.json()["cooldown_minutes"] == 10


def test_regenerate_webhook_token_invalidates_old_one(client):
    original = client.get("/ai/settings").json()["webhook_token"]
    resp = client.post("/ai/webhook/regenerate")
    assert resp.status_code == 200
    new_token = resp.json()["webhook_token"]
    assert new_token != original
    assert client.post(f"/webhook/tv/{original}", json={"symbol": "BTCUSD", "timeframe": "15m", "action": "buy"}).status_code == 404


def test_webhook_unknown_token_is_404(client):
    resp = client.post("/webhook/tv/not-a-real-token", json={"symbol": "BTCUSD", "timeframe": "15m", "action": "buy"})
    assert resp.status_code == 404


def test_webhook_disabled_is_404(client):
    token = client.get("/ai/settings").json()["webhook_token"]
    resp = client.post(f"/webhook/tv/{token}", json={"symbol": "BTCUSD", "timeframe": "15m", "action": "buy"})
    assert resp.status_code == 404


def test_webhook_enabled_queues_pipeline_and_returns_200(client):
    client.put("/ai/settings", json={"enabled": True})
    token = client.get("/ai/settings").json()["webhook_token"]
    with patch("main.ai_pipeline.run_pipeline") as mock_run:
        resp = client.post(f"/webhook/tv/{token}", json={"symbol": "BTCUSD", "timeframe": "15m", "action": "buy", "note": "n"})
    assert resp.status_code == 200
    signals = client.get("/ai/signals").json()
    assert signals[0]["status"] in ("processing", "done")


def test_webhook_duplicate_payload_is_skipped(client):
    client.put("/ai/settings", json={"enabled": True})
    token = client.get("/ai/settings").json()["webhook_token"]
    payload = {"symbol": "BTCUSD", "timeframe": "15m", "action": "buy", "note": "n"}
    with patch("main.ai_pipeline.run_pipeline"):
        client.post(f"/webhook/tv/{token}", json=payload)
        client.post(f"/webhook/tv/{token}", json=payload)
    signals = client.get("/ai/signals").json()
    assert signals[0]["status"] == "duplicate_skipped"


def test_webhook_missing_fields_is_422(client):
    client.put("/ai/settings", json={"enabled": True})
    token = client.get("/ai/settings").json()["webhook_token"]
    resp = client.post(f"/webhook/tv/{token}", json={"symbol": "BTCUSD"})
    assert resp.status_code == 422
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd /home/anand-shaurya/Documents/personal/delta/backend && .venv/bin/python -m pytest tests/test_ai_webhook.py -v`
Expected: FAIL — `404 Not Found` on `/ai/settings` (route doesn't exist yet).

- [ ] **Step 4: Implement**

Append to `backend/main.py` (add `import ai_pipeline` near the top alongside the other `import ... # noqa: E402` lines, and `from fastapi import BackgroundTasks` to the existing fastapi import line):

```python
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
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    return store.update_ai_settings(auth.user_id_var.get(), **fields)


@app.post("/ai/webhook/regenerate", tags=["ai"])
def regenerate_ai_webhook():
    token = store.regenerate_webhook_token(auth.user_id_var.get())
    return {"webhook_token": token}


@app.get("/ai/signals", tags=["ai"])
def get_ai_signals(limit: int = Query(50, ge=1, le=500)):
    return store.list_signals(auth.user_id_var.get(), limit)


def _run_signal_pipeline(signal_id: int, user_id: str, signal: dict[str, Any]) -> None:
    settings = store.get_or_create_ai_settings(user_id)
    try:
        result = ai_pipeline.run_pipeline(user_id, signal, settings)
        store.update_signal_result(signal_id, "done", result)
        if result["execution_result"].get("reason") == "guardrail:max_daily_loss":
            store.update_ai_settings(user_id, enabled=False)
    except Exception as exc:  # pragma: no cover - defensive, exercised via mocked failure in review
        store.update_signal_result(signal_id, "error", {"error": str(exc)})


@app.post("/webhook/tv/{token}", tags=["ai"])
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
```

This endpoint resolves `user_id` once at the top (from the token) — Task 2/9's Review Focus item about token regeneration mid-flight: once this request has resolved `user_id`, a concurrent regeneration doesn't affect it; only the *next* webhook call needs the new token.

`WebhookSignalIn` requiring `symbol`/`timeframe`/`action` as non-optional `str` fields gives the `422` for missing fields in `test_webhook_missing_fields_is_422` for free via Pydantic.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd /home/anand-shaurya/Documents/personal/delta/backend && .venv/bin/python -m pytest tests/test_ai_webhook.py -v`
Expected: 8 passed.

- [ ] **Step 6: Run the full backend suite**

Run: `cd /home/anand-shaurya/Documents/personal/delta/backend && .venv/bin/python -m pytest -v`
Expected: every test in the repo passes (existing + all new AI tests).

- [ ] **Step 7: Commit**

```bash
cd /home/anand-shaurya/Documents/personal/delta
git add backend/main.py backend/tests/test_ai_webhook.py
git commit -m "feat(main): add AI settings/signals endpoints and TradingView webhook"
```

---

### Task 10: `frontend/lib/aiTrading.ts` — types + pure helpers

**Files:**
- Create: `frontend/lib/aiTrading.ts`
- Test: `frontend/lib/aiTrading.test.ts`

**Interfaces:**
- Produces:
  - `type AiSettings` — mirrors the backend's `GET /ai/settings` response shape.
  - `type AiSignal` — mirrors one row from `GET /ai/signals`.
  - `validateCooldownMinutes(v: number) -> string | null`
  - `validateSymbolAllowlist(v: string[]) -> string | null`
  - `formatGuardrailSummary(settings: AiSettings) -> string` — short human-readable summary line for the page (e.g. `"3 guardrails active"` or `"No guardrails enabled"`), used in the UI header.

- [ ] **Step 1: Write the failing tests**

```ts
// frontend/lib/aiTrading.test.ts
import { expect, it } from "vitest";
import { validateCooldownMinutes, validateSymbolAllowlist, formatGuardrailSummary, type AiSettings } from "./aiTrading";

it("validates cooldown minutes", () => {
  expect(validateCooldownMinutes(0)).toMatch(/at least/i);
  expect(validateCooldownMinutes(-5)).toMatch(/at least/i);
  expect(validateCooldownMinutes(30)).toBeNull();
});

it("validates symbol allowlist", () => {
  expect(validateSymbolAllowlist([])).toMatch(/at least one symbol/i);
  expect(validateSymbolAllowlist(["BTCUSD"])).toBeNull();
});

const baseSettings: AiSettings = {
  user_id: "u1",
  webhook_token: "tok",
  enabled: true,
  cooldown_minutes: 30,
  coordinator_model: "gpt-4.1",
  data_model: "gpt-4.1-mini",
  executer_model: "gpt-4.1-mini",
  coordinator_prompt: "",
  data_prompt: "",
  executer_prompt: "",
  guardrails_json: {},
  updated_at: 0,
};

it("summarizes zero enabled guardrails", () => {
  expect(formatGuardrailSummary(baseSettings)).toBe("No guardrails enabled");
});

it("summarizes enabled guardrail count", () => {
  const settings: AiSettings = {
    ...baseSettings,
    guardrails_json: {
      max_position_size: { enabled: true, value: 50 },
      symbol_allowlist: { enabled: false, value: [] },
      max_trades_per_day: { enabled: true, value: 10 },
    },
  };
  expect(formatGuardrailSummary(settings)).toBe("2 guardrails active");
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /home/anand-shaurya/Documents/personal/delta/frontend && npx vitest run lib/aiTrading.test.ts`
Expected: FAIL — cannot find module `./aiTrading`.

- [ ] **Step 3: Implement**

```ts
// frontend/lib/aiTrading.ts
export type GuardrailConfig = { enabled: boolean; value: number | string[] };

export type AiSettings = {
  user_id: string;
  webhook_token: string;
  enabled: boolean;
  cooldown_minutes: number;
  coordinator_model: string;
  data_model: string;
  executer_model: string;
  coordinator_prompt: string;
  data_prompt: string;
  executer_prompt: string;
  guardrails_json: Record<string, GuardrailConfig>;
  updated_at: number;
};

export type AiSignal = {
  id: number;
  ts: number;
  symbol: string;
  timeframe: string;
  action: string;
  note: string | null;
  status: "received" | "duplicate_skipped" | "cooldown_skipped" | "processing" | "done" | "error";
  result_json: Record<string, any> | null;
};

export function validateCooldownMinutes(v: number): string | null {
  if (!Number.isFinite(v) || v < 1) return "Cooldown must be at least 1 minute.";
  return null;
}

export function validateSymbolAllowlist(v: string[]): string | null {
  if (v.length === 0) return "Pick at least one symbol for the allowlist, or disable it.";
  return null;
}

export function formatGuardrailSummary(settings: AiSettings): string {
  const count = Object.values(settings.guardrails_json).filter((g) => g.enabled).length;
  return count === 0 ? "No guardrails enabled" : `${count} guardrail${count === 1 ? "" : "s"} active`;
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /home/anand-shaurya/Documents/personal/delta/frontend && npx vitest run lib/aiTrading.test.ts`
Expected: 4 passed.

- [ ] **Step 5: Typecheck**

Run: `cd /home/anand-shaurya/Documents/personal/delta/frontend && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
cd /home/anand-shaurya/Documents/personal/delta
git add frontend/lib/aiTrading.ts frontend/lib/aiTrading.test.ts
git commit -m "feat(frontend): add AI trading types and pure validation helpers"
```

---

### Task 11: `frontend/app/(app)/ai-trading/page.tsx` — settings + decision log UI

**Files:**
- Create: `frontend/app/(app)/ai-trading/page.tsx`
- Modify: wherever the app's nav/sidebar links are defined (find it by grepping for the existing `/analysis` or `/settings` link in the frontend — add an `/ai-trading` link next to them, following the same pattern).

**Interfaces:**
- Consumes: `api()` from `frontend/lib/api.ts`, `AiSettings`/`AiSignal`/`validateCooldownMinutes`/`validateSymbolAllowlist`/`formatGuardrailSummary` from Task 10, `Chip`/`Stat` from `frontend/components/Ui.tsx`, `useToast` from `frontend/components/Toast`.

- [ ] **Step 1: Find the nav pattern**

Run: `cd /home/anand-shaurya/Documents/personal/delta/frontend && grep -rln "/analysis" app/(app) components 2>/dev/null`

Read whichever file defines the nav links (likely a layout or a `Nav`/`Shell` component) to copy its exact link markup for the new `/ai-trading` entry.

- [ ] **Step 2: Build the page**

```tsx
// frontend/app/(app)/ai-trading/page.tsx
"use client";
import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { useToast } from "@/components/Toast";
import { Chip } from "@/components/Ui";
import {
  type AiSettings,
  type AiSignal,
  validateCooldownMinutes,
  validateSymbolAllowlist,
  formatGuardrailSummary,
} from "@/lib/aiTrading";

const ALL_SYMBOLS = ["BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "DOGEUSD"];
const MODELS = ["gpt-4.1", "gpt-4.1-mini", "gpt-4o", "gpt-4o-mini"];

export default function AiTradingPage() {
  const toast = useToast();
  const [settings, setSettings] = useState<AiSettings | null>(null);
  const [signals, setSignals] = useState<AiSignal[]>([]);
  const [expanded, setExpanded] = useState<number | null>(null);

  async function load() {
    try {
      const [s, sigs] = await Promise.all([
        api<AiSettings>("GET", "/ai/settings"),
        api<AiSignal[]>("GET", "/ai/signals?limit=50"),
      ]);
      setSettings(s);
      setSignals(sigs);
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "Failed to load AI trading settings", "err");
    }
  }

  useEffect(() => {
    load();
    const id = setInterval(load, 15000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function save(patch: Partial<AiSettings>) {
    if (!settings) return;
    try {
      const updated = await api<AiSettings>("PUT", "/ai/settings", patch);
      setSettings(updated);
      toast("Saved", "ok");
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "Failed to save", "err");
    }
  }

  async function regenerate() {
    if (!confirm("Regenerate the webhook URL? The old URL will stop working immediately.")) return;
    const { webhook_token } = await api<{ webhook_token: string }>("POST", "/ai/webhook/regenerate");
    setSettings((s) => (s ? { ...s, webhook_token } : s));
    toast("Webhook URL regenerated", "ok");
  }

  function toggleGuardrail(name: string, enabled: boolean) {
    if (!settings) return;
    const current = settings.guardrails_json[name] ?? { enabled: false, value: name === "symbol_allowlist" ? [] : 0 };
    save({ guardrails_json: { ...settings.guardrails_json, [name]: { ...current, enabled } } });
  }

  function setGuardrailValue(name: string, value: number | string[]) {
    if (!settings) return;
    const current = settings.guardrails_json[name] ?? { enabled: false, value };
    save({ guardrails_json: { ...settings.guardrails_json, [name]: { ...current, value } } });
  }

  if (!settings) return <div>Loading…</div>;

  const webhookUrl = typeof window !== "undefined" ? `${window.location.origin}/api/webhook/tv/${settings.webhook_token}` : "";
  const allowlist = (settings.guardrails_json.symbol_allowlist?.value as string[] | undefined) ?? [];
  const cooldownError = validateCooldownMinutes(settings.cooldown_minutes);
  const allowlistError = settings.guardrails_json.symbol_allowlist?.enabled ? validateSymbolAllowlist(allowlist) : null;

  return (
    <div>
      <section>
        <h2>AI Trading</h2>
        <p className="muted">{formatGuardrailSummary(settings)}</p>

        <div className="autoRow" style={{ alignItems: "center", gap: 10 }}>
          <label style={{ flexDirection: "row", alignItems: "center", gap: 6 }}>
            <input type="checkbox" checked={settings.enabled} onChange={(e) => save({ enabled: e.target.checked })} />
            Enable AI trading
          </label>
          <label>
            Cooldown (minutes)
            <input
              type="number"
              min={1}
              style={{ width: 70 }}
              value={settings.cooldown_minutes}
              onChange={(e) => save({ cooldown_minutes: +e.target.value })}
            />
          </label>
        </div>
        {cooldownError && <p className="err">{cooldownError}</p>}

        <div className="autoRow" style={{ marginTop: 10 }}>
          <code style={{ flex: 1, wordBreak: "break-all" }}>{webhookUrl}</code>
          <button type="button" className="btn ghost sm" onClick={() => navigator.clipboard.writeText(webhookUrl)}>
            Copy
          </button>
          <button type="button" className="btn ghost sm" onClick={regenerate}>
            Regenerate
          </button>
        </div>

        <h3 style={{ marginTop: 20 }}>Guardrails</h3>
        <div className="autoRow" style={{ flexDirection: "column", gap: 10 }}>
          <label style={{ flexDirection: "row", alignItems: "center", gap: 6 }}>
            <input
              type="checkbox"
              checked={!!settings.guardrails_json.max_position_size?.enabled}
              onChange={(e) => toggleGuardrail("max_position_size", e.target.checked)}
            />
            Max position size
            <input
              type="number"
              style={{ width: 80 }}
              value={(settings.guardrails_json.max_position_size?.value as number) ?? 0}
              onChange={(e) => setGuardrailValue("max_position_size", +e.target.value)}
            />
          </label>
          <label style={{ flexDirection: "row", alignItems: "center", gap: 6 }}>
            <input
              type="checkbox"
              checked={!!settings.guardrails_json.max_trades_per_day?.enabled}
              onChange={(e) => toggleGuardrail("max_trades_per_day", e.target.checked)}
            />
            Max trades per day
            <input
              type="number"
              style={{ width: 80 }}
              value={(settings.guardrails_json.max_trades_per_day?.value as number) ?? 0}
              onChange={(e) => setGuardrailValue("max_trades_per_day", +e.target.value)}
            />
          </label>
          <label style={{ flexDirection: "row", alignItems: "center", gap: 6 }}>
            <input
              type="checkbox"
              checked={!!settings.guardrails_json.max_daily_loss?.enabled}
              onChange={(e) => toggleGuardrail("max_daily_loss", e.target.checked)}
            />
            Max daily loss (USD)
            <input
              type="number"
              style={{ width: 80 }}
              value={(settings.guardrails_json.max_daily_loss?.value as number) ?? 0}
              onChange={(e) => setGuardrailValue("max_daily_loss", +e.target.value)}
            />
          </label>
          <label style={{ flexDirection: "row", alignItems: "center", gap: 6 }}>
            <input
              type="checkbox"
              checked={!!settings.guardrails_json.symbol_allowlist?.enabled}
              onChange={(e) => toggleGuardrail("symbol_allowlist", e.target.checked)}
            />
            Symbol allowlist
          </label>
          <div className="chips">
            {ALL_SYMBOLS.map((s) => (
              <Chip
                key={s}
                on={allowlist.includes(s)}
                onClick={() => setGuardrailValue("symbol_allowlist", allowlist.includes(s) ? allowlist.filter((x) => x !== s) : [...allowlist, s])}
              >
                {s}
              </Chip>
            ))}
          </div>
          {allowlistError && <p className="err">{allowlistError}</p>}
        </div>

        <h3 style={{ marginTop: 20 }}>Agent instructions</h3>
        {(["coordinator", "data", "executer"] as const).map((agent) => (
          <div key={agent} style={{ marginBottom: 14 }}>
            <label>
              {agent} model
              <select
                value={settings[`${agent}_model` as const]}
                onChange={(e) => save({ [`${agent}_model`]: e.target.value } as Partial<AiSettings>)}
              >
                {MODELS.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
            </label>
            <textarea
              rows={3}
              style={{ width: "100%" }}
              placeholder={`Extra instructions for the ${agent} agent`}
              defaultValue={settings[`${agent}_prompt` as const]}
              onBlur={(e) => save({ [`${agent}_prompt`]: e.target.value } as Partial<AiSettings>)}
            />
          </div>
        ))}

        <h3 style={{ marginTop: 20 }}>Decision log</h3>
        <table>
          <thead>
            <tr>
              <th>Time</th>
              <th>Symbol</th>
              <th>TF</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {signals.map((s) => (
              <>
                <tr key={s.id} onClick={() => setExpanded(expanded === s.id ? null : s.id)} style={{ cursor: "pointer" }}>
                  <td>{new Date(s.ts * 1000).toLocaleString()}</td>
                  <td>{s.symbol}</td>
                  <td>{s.timeframe}</td>
                  <td>{s.status}</td>
                </tr>
                {expanded === s.id && (
                  <tr key={`${s.id}-detail`}>
                    <td colSpan={4}>
                      <pre style={{ whiteSpace: "pre-wrap" }}>{JSON.stringify(s.result_json, null, 2)}</pre>
                    </td>
                  </tr>
                )}
              </>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  );
}
```

- [ ] **Step 3: Add the nav link**

Using whatever file Step 1 found, add an `/ai-trading` entry next to the existing `/analysis` link, matching that file's exact markup/pattern (component name and props depend on what Step 1 reveals — mirror it exactly rather than introducing a new nav pattern).

- [ ] **Step 4: Typecheck and run the full frontend test suite**

Run: `cd /home/anand-shaurya/Documents/personal/delta/frontend && npx tsc --noEmit && npx vitest run`
Expected: no type errors; all tests (existing + Task 10's) pass. This page itself has no dedicated test file, matching the existing convention where `frontend/app/(app)/analysis/page.tsx` also has no test file — only its `lib/*.ts` logic is tested.

- [ ] **Step 5: Manually verify in the browser**

Run: start the backend (`cd backend && .venv/bin/uvicorn main:app --reload --port 8000`) and frontend (`cd frontend && npm run dev`) locally, sign in, open `/ai-trading`, confirm the page loads, toggles persist after a refresh, and the webhook URL copy button works. (A real TradingView-triggered end-to-end test isn't possible without a funded Delta Exchange account and OpenAI key configured — defer that manual check to when the user is ready to test it live.)

- [ ] **Step 6: Commit**

```bash
cd /home/anand-shaurya/Documents/personal/delta
git add frontend/app/\(app\)/ai-trading/page.tsx
git add -u
git commit -m "feat(frontend): add AI Trading settings and decision log page"
```

---

## After all tasks

Run the full test suite one more time end to end (`backend`: `.venv/bin/python -m pytest -v`; `frontend`: `npx vitest run && npx tsc --noEmit`) before moving to `finishing-a-development-branch`. Remember: this plan is explicitly local-only (per Global Constraints) — no Pi deployment or systemd changes are part of this work.
