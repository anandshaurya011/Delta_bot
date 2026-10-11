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
  status text not null,       -- received | cooldown_skipped | processing | done | error
  result_json text            -- plan, guardrail checks, execution result, reasoning
);
create index if not exists ai_sig_user on ai_signals(user_id, id);
create index if not exists ai_sig_cooldown on ai_signals(user_id, timeframe, ts);

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

Handling, in order:

1. Insert a row into `ai_signals` with `status = "received"`.
2. If `ai_settings.enabled` is false → mark `status = "error"` with a reason,
   return `404` (shouldn't normally happen since the token lookup already
   filters on `enabled`, but double-checked here in case settings changed
   mid-flight).
3. Cooldown check, scoped to `(user_id, timeframe)`: find the most recent
   `ai_signals` row for this user+timeframe where `status = "done"` **and**
   `result_json.execution_result.executed = true` (i.e. an actual trade was
   placed, not a hold/reject). If `now - that_row.ts < cooldown_minutes`,
   mark this new row `status = "cooldown_skipped"` and return `200`
   immediately — no agents invoked.
4. Otherwise, launch the LangGraph pipeline via FastAPI `BackgroundTasks`,
   set `status = "processing"`, and return `200` immediately. The background
   task updates the row to `status = "done"` or `"error"` with the full
   `result_json` once the pipeline finishes.

Only a **successfully executed trade** starts the cooldown clock for that
timeframe. A hold/reject leaves that timeframe immediately available for the
next signal.

Single-flight note: because TradingView may fire the same alert multiple
times in quick succession, and the pipeline for one signal can take several
seconds, the cooldown check in step 3 does not prevent two signals for the
same timeframe from both reaching "processing" concurrently if they arrive
within that window before the first one finishes. This is accepted as a rare
edge case for this phase (local, low-volume testing) and not specially
guarded against; it can be revisited if it causes real duplicate trades.

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
  base + `ai_settings.executer_prompt`. Input: coordinator's `plan` +
  current guardrail config. Steps:
  1. Evaluate every *enabled* guardrail against the plan (see §6). Any
     violation → stop, mark `execution_result = {executed: false, reason: "guardrail:<name>"}`.
     Never silently resize/clip a plan to fit — reject outright.
  2. If all guardrails pass, call the appropriate `delta_api.py` trade
     function (buy/sell/close/etc., matching the existing endpoints already
     used by `main.py`) and record
     `execution_result = {executed: true, order: {...}}`.

Every node's input/output is accumulated into the signal's `result_json`,
so the decision log can show the full chain: raw signal → data gathered →
plan + reasoning → guardrail checks → execution outcome.

## 6. Guardrail enforcement (executer-side, all user-toggleable)

| Guardrail | Enforcement |
|---|---|
| `max_position_size` | Reject if the plan's resulting position size (existing + size_hint) would exceed `value` contracts. |
| `max_trades_per_day` | Reject if `ai_trade_count` for `(user_id, today UTC)` is already `>= value`. Incremented only after a successful `executed: true` trade. |
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

## 10. Testing

- Backend: unit tests for —
  - `store.py` new tables (settings round-trip, signal insert/list, trade
    count increment).
  - Cooldown logic: per-`(user_id, timeframe)` scoping, only
    `executed: true` rows start the clock, independent timeframes unaffected.
  - Each guardrail's rejection path, and the `max_daily_loss` kill-switch
    side effect on `ai_settings.enabled`.
  - The LangGraph pipeline nodes with the OpenAI client mocked — no real
    API calls in tests.
- Frontend: Vitest component test for the new settings page's load/save
  round trip and guardrail toggle behavior, following existing patterns in
  `frontend/tests`.

## 11. Explicitly deferred (not in this phase)

- Per-user OpenAI keys.
- Pi deployment / systemd services for this feature.
- Strict single-flight locking beyond the cooldown approximation in §4.
