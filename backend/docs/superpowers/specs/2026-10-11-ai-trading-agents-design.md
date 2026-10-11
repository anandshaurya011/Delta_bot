# AI-powered TradingView signal execution — design spec

Date: 2026-10-11
Status: approved, pending implementation plan
Scope: local development only. No Raspberry Pi deployment as part of this work.

## 1. Goal

Let a user wire a TradingView alert to a per-user webhook URL. Each incoming
signal is handled by a 3-agent LangGraph pipeline (coordinator, data_agent,
executer) that decides whether to open/add/reduce/flip/hold/close a position
on Delta Exchange, subject to user-configured guardrails, and places the
trade autonomously — no human confirmation step. Every signal and decision is
logged for the user to review.

## 2. Out of scope

- Raspberry Pi / production deployment of this feature.
- Per-user OpenAI keys (one shared `OPENAI_API_KEY` for now).
- Any UI/alerting beyond the new AI Trading page and existing toast/log
  patterns.
- Historical backtesting of agent decisions.

## 3. Data model (added to `backend/store.py`, same SQLite file)

```sql
create table if not exists ai_settings(
  user_id text primary key,
  webhook_token text unique not null,
  enabled integer not null default 0,
  cooldown_minutes integer not null default 30,
  coordinator_model text not null default 'gpt-4.1',
  data_model text not null default 'gpt-4.1-mini',
  executer_model text not null default 'gpt-4.1-mini',
  coordinator_prompt text not null default '',
  data_prompt text not null default '',
  executer_prompt text not null default '',
  guardrails_json text not null default '{}',
  updated_at integer not null
);

create table if not exists ai_signals(
  id integer primary key autoincrement,
  user_id text not null,
  ts integer not null,
  symbol text not null,
  timeframe text not null,
  action text not null,       -- raw action from the TradingView payload
  note text,
  dedup_key text not null,    -- sha256(user_id|symbol|timeframe|action|note), see §4
  status text not null,       -- received | duplicate_skipped | cooldown_skipped | processing | done | error
  result_json text            -- plan, guardrail checks, execution result, reasoning
);
create index if not exists ai_sig_user on ai_signals(user_id, id);
create index if not exists ai_sig_cooldown on ai_signals(user_id, timeframe, ts);
create index if not exists ai_sig_dedup on ai_signals(user_id, dedup_key, ts);

create table if not exists ai_trade_count(
  user_id text not null,
  day text not null,           -- 'YYYY-MM-DD', UTC
  count integer not null default 0,
  primary key (user_id, day)
);
```

`guardrails_json` shape:

```json
{
  "max_position_size": {"enabled": true, "value": 50},
  "max_trades_per_day": {"enabled": true, "value": 10},
  "symbol_allowlist": {"enabled": true, "value": ["BTCUSD", "ETHUSD"]},
  "max_daily_loss": {"enabled": true, "value": 200}
}
```

A disabled guardrail (`enabled: false`) is skipped entirely during
enforcement, regardless of `value`.

`webhook_token` is a random URL-safe token (`secrets.token_urlsafe(24)`),
generated the first time a user opens the AI Trading page, and replaceable
via a regenerate action.

## 4. Webhook endpoint

`POST /api/webhook/tv/{token}` — no Clerk auth. The path token is looked up
against `ai_settings.webhook_token` to resolve `user_id`; unknown token or
`enabled = 0` → `404`.

Request body:

```json
{"symbol": "BTCUSD", "action": "buy", "timeframe": "15m", "note": "optional free text"}
```

`action` is a free-form string the coordinator interprets (e.g. `buy`,
`sell`, `close`, `alert`) — not a closed enum, since TradingView alert
templates vary.

### Duplicate-signal protection (hard requirement, not best-effort)

TradingView is documented to retry alert deliveries, and a flaky network can
cause the same alert to arrive more than once. Two independent mechanisms
guard against executing the same signal twice — dedup must be resolved
**before** any background task starts, inside the request handler, under a
single SQLite transaction, so two near-simultaneous deliveries can't both
pass the check:

1. **Idempotency key**: `dedup_key = sha256(f"{user_id}|{symbol}|{timeframe}|{action}|{note}")`.
   On receipt, within one `BEGIN IMMEDIATE` transaction: look up
   `ai_signals` for this `(user_id, dedup_key)` with `ts` within the last
   `dedup_window_seconds` (fixed at 120s — generous enough for TradingView's
   retry window, short enough not to block a genuinely repeated alert later).
   If found, insert the new row as `status = "duplicate_skipped"` and return
   `200` without touching the pipeline.
2. **Single-flight per `(user_id, timeframe)`**: within the same
   transaction, if any existing row for this `(user_id, timeframe)` has
   `status = "processing"`, insert the new row as `status = "duplicate_skipped"`
   and return `200`. This closes the race the original draft of this spec
   left open (two signals both reaching "processing" before either
   finished) — only one signal per timeframe is ever in flight.

Both checks run in the same transaction as the row insert (`INSERT ...
SELECT NOT EXISTS (...)` pattern or an explicit lock), so there is no
check-then-insert race between concurrent requests.

Handling, in order:

1. Within one SQLite transaction: run the dedup and single-flight checks
   above. If either trips, insert the row as `duplicate_skipped` and return
   `200` — no agents invoked, no further steps run.
2. If `ai_settings.enabled` is false → insert as `status = "error"`, return
   `404` (shouldn't normally happen since the token lookup already filters
   on `enabled`, but double-checked here in case settings changed
   mid-flight).
3. Cooldown check, scoped to `(user_id, timeframe)`: find the most recent
   `ai_signals` row for this user+timeframe where `status = "done"` **and**
   `result_json.execution_result.executed = true` (i.e. an actual trade was
   placed and confirmed filled, not a hold/reject — see §5 on fill
   verification). If `now - that_row.ts < cooldown_minutes`, mark this new
   row `status = "cooldown_skipped"` and return `200` immediately — no
   agents invoked.
4. Otherwise, insert the row as `status = "processing"` (this insert is what
   step 1's single-flight check on future requests will see), launch the
   LangGraph pipeline via FastAPI `BackgroundTasks`, and return `200`
   immediately. The background task updates the row to `status = "done"` or
   `"error"` with the full `result_json` once the pipeline finishes.

Only a **successfully executed and filled trade** starts the cooldown clock
for that timeframe. A hold/reject/guardrail-rejection leaves that timeframe
immediately available for the next signal.

## 5. LangGraph pipeline

Three nodes, each a single LLM call with tool access, run through LangGraph:

- **coordinator** — model: `ai_settings.coordinator_model`, system prompt
  built from a fixed base prompt + `ai_settings.coordinator_prompt`. Input:
  the raw signal. It can call the `request_data` tool (delegates to
  data_agent) before producing a final `plan`:
  `{action: open|add|reduce|flip|hold|close, side: long|short|none, size_hint: number|null, reasoning: string}`.
- **data_agent** — model: `ai_settings.data_model`, system prompt = fixed
  base + `ai_settings.data_prompt`. Tools are thin Python wrappers calling
  the existing `delta_api.py` functions **in-process** (not HTTP) using the
  requesting user's stored Delta credentials: current position for the
  symbol, latest analysis/ADX/RSI, price, balances, open orders. Returns
  structured JSON back to the coordinator.
- **executer** — model: `ai_settings.executer_model`, system prompt = fixed
  base + `ai_settings.executer_prompt`. The LLM's role here is limited to
  translating the coordinator's `plan` into a concrete order request
  (symbol, side, size, order type) and writing the human-readable summary
  stored in the log. It does **not** decide whether the trade is allowed —
  see below.
  1. **Guardrail evaluation is plain deterministic Python, not an LLM
     judgment call.** A pure function
     `evaluate_guardrails(plan, guardrails_json, context) -> GuardrailResult`
     runs against the order request the LLM produced, using the same
     numeric checks as §6, entirely in Python with no model in the loop.
     The LLM is never asked "is this allowed" and its output is never
     trusted as the allow/deny decision. Any violation → stop, mark
     `execution_result = {executed: false, reason: "guardrail:<name>"}`.
     Never silently resize/clip a plan to fit — reject outright.
  2. If `evaluate_guardrails` passes, call the appropriate `delta_api.py`
     trade function (buy/sell/close/etc., matching the existing endpoints
     already used by `main.py`) to place the order.
  3. **Verify the order actually filled — acceptance by Delta's API is not
     completion.** After placing the order, poll `delta_api.py`'s order-status
     lookup (the same function backing the existing `/orders`/`/history`
     endpoints) for that order id, up to a fixed timeout (10s, 1s interval).
     Record the final observed state:
     - Filled (fully or, for the recorded size, partially) →
       `execution_result = {executed: true, order: {...}, fill_status: "filled"|"partially_filled", filled_size: N}`.
       Only a `filled`/`partially_filled` outcome counts as "executed" for
       the cooldown clock (§4) and the `max_trades_per_day` counter (§6).
     - Still open/pending after the timeout → `execution_result = {executed: false, reason: "unfilled_timeout", order: {...}}`.
       The order is left live on the exchange (not auto-cancelled); the log
       flags it clearly so the user can check it manually. This does **not**
       start the cooldown, since no confirmed trade happened yet.
     - Rejected by the exchange → `execution_result = {executed: false, reason: "exchange_rejected", order: {...}}`.

Every node's input/output is accumulated into the signal's `result_json`,
so the decision log can show the full chain: raw signal → data gathered →
plan + reasoning → guardrail checks (Python, with pass/fail per rule) →
order placement → fill verification → final outcome.

## 6. Guardrail enforcement (executer-side, all user-toggleable)

| Guardrail | Enforcement |
|---|---|
| `max_position_size` | Reject if the plan's resulting position size (existing + size_hint) would exceed `value` contracts. |
| `max_trades_per_day` | Reject if `ai_trade_count` for `(user_id, today UTC)` is already `>= value`. Incremented only after a trade is confirmed `filled`/`partially_filled` (§5 step 3) — an unfilled or exchange-rejected order does not count. |
| `symbol_allowlist` | Reject (before data_agent runs, to save an LLM call) if the signal's symbol is not in `value`. |
| `max_daily_loss` | Compute today's realized+unrealized P&L via existing balance/position endpoints; if loss exceeds `value`, reject this signal **and** set `ai_settings.enabled = 0` (kill switch) so the whole webhook goes inactive until the user manually re-enables it. |

## 7. New backend API (Clerk-authed, added to `backend/main.py`)

- `GET /api/ai/settings` — returns the user's `ai_settings` row (creating a
  default disabled row with a fresh token on first access).
- `PUT /api/ai/settings` — update models, prompts, guardrails, cooldown
  minutes, enabled flag.
- `POST /api/ai/webhook/regenerate` — issues a new `webhook_token`,
  invalidating the old one.
- `GET /api/ai/signals?limit=` — paginated decision log for the AI Trading
  page (signal, status, full `result_json`).
- `POST /api/webhook/tv/{token}` — the TradingView-facing endpoint (§4).

## 8. Frontend

New `/ai-trading` page (added to the existing Next.js app under
`frontend/app/(app)/`):

- Webhook URL display (built from the token) with copy button and a
  "Regenerate" action (confirms before invalidating the old URL).
- Enable/disable toggle for the whole pipeline.
- Cooldown-minutes numeric input.
- One card per guardrail: enable toggle + its threshold input(s)
  (`symbol_allowlist` is a multi-select/chip list reusing the `Chip`
  component already used on the Analysis page).
- Three textareas — one per agent — for custom instructions, each with a
  model dropdown (`gpt-4.1`, `gpt-4.1-mini`, and any other models the user
  wants added to the list).
- A decision-log table (reusing existing table/list patterns) showing
  recent signals: timestamp, symbol, timeframe, status, and an expandable
  row for the full reasoning/plan/execution JSON.

## 9. Error handling

- Malformed webhook body (missing `symbol`/`timeframe`) → `422`, no
  `ai_signals` row created (nothing to act on).
- Unknown/disabled token → `404`.
- LLM call failure or tool-call error inside the pipeline → caught in the
  background task, row marked `status = "error"` with the exception message
  in `result_json`, no trade placed.
- Guardrail rejection is not an error — it's a normal `done` outcome with
  `executed: false`.
- An order that times out unfilled, or is rejected by the exchange, is also
  a normal `done` outcome with `executed: false` (reasons `unfilled_timeout`
  / `exchange_rejected`) — not a pipeline `error`, since the pipeline itself
  worked correctly.
- Duplicate/single-flight skips (§4) are a normal `done`-equivalent outcome
  (`status = "duplicate_skipped"`), logged but not treated as an error.

## 10. Testing

- Backend: unit tests for —
  - `store.py` new tables (settings round-trip, signal insert/list, trade
    count increment).
  - Cooldown logic: per-`(user_id, timeframe)` scoping, only
    `executed: true` rows start the clock, independent timeframes unaffected.
  - Each guardrail's rejection path (as plain Python function tests, not
    requiring an LLM), and the `max_daily_loss` kill-switch side effect on
    `ai_settings.enabled`.
  - Duplicate-signal and single-flight dedup: same payload twice →
    second is `duplicate_skipped`; a `processing` row blocks a same-timeframe
    signal from starting a second pipeline run.
  - Fill verification: filled / partially filled / unfilled-timeout /
    exchange-rejected paths, each mocked at the `delta_api.py` order-status
    call, asserting the correct `execution_result` and that only
    filled/partially-filled trades increment `ai_trade_count` or start the
    cooldown.
  - The LangGraph pipeline nodes with the OpenAI client mocked — no real
    API calls in tests.
- Frontend: Vitest component test for the new settings page's load/save
  round trip and guardrail toggle behavior, following existing patterns in
  `frontend/tests`.

## 11. Explicitly deferred (not in this phase)

- Per-user OpenAI keys.
- Pi deployment / systemd services for this feature.
- Auto-cancelling an order that times out unfilled (left to the user to
  handle manually; the log flags it).
