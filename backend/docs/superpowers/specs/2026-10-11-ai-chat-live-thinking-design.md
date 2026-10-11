# AI coordinator chat + live agent thinking — design spec

Date: 2026-10-11
Status: approved, pending implementation plan
Scope: local development only, continuing on branch `feat/ai-trading-agents`. Builds
directly on the already-shipped AI trading pipeline (`backend/ai_pipeline.py`,
`backend/ai_guardrails.py`, `backend/store.py`'s `ai_settings`/`ai_signals` tables,
`POST /webhook/tv/{token}`).

## 1. Goal

Add a second, user-facing entry point into the same AI trading pipeline: a chat with
the coordinator agent. The user can ask questions (answered using live data, no trade
risk) or give trade instructions in plain language ("go long BTC on the 15m") that flow
through the exact same guardrail/dedup/fill-verification path as a TradingView webhook
signal — chat is not a shortcut around safety. While either a chat-triggered trade or a
webhook-triggered one is being processed, the user can watch the three agents' reasoning
stream in live, token by token, on a new page.

## 2. Out of scope

- Multi-session chat (one continuous chat history per user, like the existing per-user
  audit log — no session picker/switcher).
- Multi-worker/horizontal scaling of the SSE mechanism (single uvicorn process only,
  same as the current deployment model).
- Any change to the webhook (`POST /webhook/tv/{token}`) path's behavior. It must work
  identically after this change — same dedup/cooldown/guardrail/fill-verification
  guarantees, same tests passing unmodified.
- Voice or any non-text input.

## 3. Hard safety requirement

A chat message that resolves to a trade instruction must go through
`store.record_incoming_signal` (the same dedup/single-flight/cooldown transaction the
webhook uses) and `ai_guardrails.evaluate_guardrails` (the same plain-Python allow/deny
check) before anything is executed. There is no separate, lighter-weight trading path
for chat. A chat message that is just a question must never reach
`ai_guardrails.evaluate_guardrails` or `delta_api.place_order` at all.

## 4. Data model additions (`backend/store.py`)

```sql
create table if not exists ai_chat_messages(
  id integer primary key autoincrement,
  user_id text not null,
  ts integer not null,
  role text not null,          -- 'user' | 'assistant'
  content text not null,
  signal_id integer            -- references ai_signals.id when this message triggered
                                -- (or reported the outcome of) a real trade; null for Q&A
);
create index if not exists ai_chat_user on ai_chat_messages(user_id, id);
```

New `store.py` functions:
- `add_chat_message(user_id: str, role: str, content: str, signal_id: int | None = None) -> int`
  — inserts a row, returns its id.
- `list_chat_messages(user_id: str, limit: int = 100) -> list[dict]` — oldest-first (chat
  reading order), capped at `limit` most recent.

## 5. Streaming support in `ai_pipeline.py` (backward compatible)

`call_llm` gains an optional `on_chunk: Callable[[str], None] | None = None` parameter.
When provided, the OpenAI call is made with `stream=True`; each text delta is passed to
`on_chunk` as it arrives, and the full accumulated text is still returned at the end
(unchanged return type/contract for every existing caller). When `on_chunk` is `None`
(every existing call site — `coordinator_node`, `data_agent_node`, `executer_node` as
called from `run_pipeline`/the webhook path), behavior is byte-for-byte identical to
today: a single non-streaming `stream=False` call, same return value.

Each of `coordinator_node`, `data_agent_node`, `executer_node` gains an optional
`on_event: Callable[[dict], None] | None = None` parameter, default `None`, threaded
through to `call_llm` as `on_chunk=lambda text: on_event({"node": <name>, "type": "token", "text": text})`
when `on_event` is provided. Each node also emits non-token progress events through the
same callback when provided (e.g. `data_agent_node` emits
`{"node": "data_agent", "type": "tool_call", "name": "get_position", "args": {...}}`
before executing each tool; `executer_node` emits
`{"node": "executer", "type": "guardrail_result", "allowed": bool, "reason": str | None}`
after `evaluate_guardrails` runs). `run_pipeline` gains the same optional `on_event`
parameter, threaded into each node call; when `None`, its behavior and return value are
unchanged from today.

**Review focus for this spec's implementation:** every existing test in
`backend/tests/test_ai_pipeline.py` and `backend/tests/test_ai_webhook.py` must still
pass unmodified, proving `on_event=None`/`on_chunk=None` truly changes nothing for the
webhook path.

## 6. `backend/ai_chat.py` (new module)

- `resolve_chat_intent(message: str, settings: dict) -> dict` — one short, non-streamed
  LLM call (model: `settings["coordinator_model"]`) that classifies the message and
  returns either `{"intent": "trade", "symbol": str, "timeframe": str, "action": str}`
  or `{"intent": "question"}`. If the message names a trade action but omits a
  timeframe, the model is instructed to default to `"15m"` rather than ask a follow-up
  question (keeps the flow non-interactive-within-a-single-message, matching how a
  webhook signal always carries a complete payload).
- `handle_chat_message(user_id: str, message: str, settings: dict, on_event: Callable[[dict], None]) -> None`
  — the chat orchestrator. Steps, in order:
  1. `store.add_chat_message(user_id, "user", message)`.
  2. `ai_pipeline.bind_user_credentials(user_id)`.
  3. Run `ai_pipeline.data_agent_node` (with `on_event`) against a signal shaped from
     the raw message (`{"symbol": "", "timeframe": "", "action": "", "note": message}`)
     so the agent has live market/position context available for either a question or a
     trade decision. Emits `data_agent` events via `on_event` as usual.
  4. `intent = resolve_chat_intent(message, settings)`.
  5. **If `intent["intent"] == "question"`:** call `ai_pipeline.call_llm` directly (not
     through a node) with a short "answer the user's question using the gathered data"
     system prompt, `on_chunk` wired to emit `{"node": "coordinator", "type": "token", ...}`
     events, append the full reply via `store.add_chat_message(user_id, "assistant", reply)`,
     emit a final `{"type": "done"}` event, and return. No guardrails, no
     `record_incoming_signal`, no execution — this path can never place a trade.
  6. **If `intent["intent"] == "trade"`:** build a signal dict from the resolved
     `symbol`/`timeframe`/`action` plus `note=message`, call
     `store.record_incoming_signal(user_id, symbol, timeframe, action, message, settings["cooldown_minutes"])`
     — the SAME function the webhook uses, so dedup/single-flight/cooldown apply
     identically. If the returned status is not `"processing"` (i.e.
     `duplicate_skipped`/`cooldown_skipped`), emit an event explaining that and an
     assistant chat message saying so, and return — no pipeline run. If `"processing"`,
     run `ai_pipeline.coordinator_node` then `ai_pipeline.executer_node` (both with
     `on_event`, both operating on the same `PipelineState` already populated with
     `gathered_data` from step 3 — skip calling `data_agent_node` again, it already ran),
     call `store.update_signal_result(signal_id, "done"|"error", result)` exactly as
     `main.py`'s `_run_signal_pipeline` does today (including the `max_daily_loss`
     kill-switch check), append an assistant chat message summarizing the
     plan/execution outcome with `signal_id` set, emit a final `{"type": "done"}` event.

This reuses every existing safety mechanism (`record_incoming_signal`,
`evaluate_guardrails` via `executer_node`, fill verification via `wait_for_fill`) rather
than reimplementing any of it.

## 7. SSE event delivery (in-process, single-worker)

A module-level `dict[str, asyncio.Queue]` in `backend/main.py` (or a small new
`backend/ai_chat_stream.py`), keyed by `user_id`, created lazily on first subscribe.
`handle_chat_message`'s `on_event` callback pushes each event dict onto the user's
queue (`queue.put_nowait(json.dumps(event))`, dropping the oldest item if the queue
exceeds a bounded size — e.g. 200 — rather than blocking, since a slow/disconnected
client must never stall a real trade's execution). Explicitly documented limitation:
this only works correctly with a single backend process; a future multi-worker
deployment would need Redis pub/sub or similar — out of scope here, same as the rest of
this feature being local-only for now.

## 8. New backend API (`backend/main.py`)

- `POST /ai/chat` (Clerk-authed) — body `{"message": str}`. Validates `ai_settings.enabled`
  (chat trading is gated by the same enable toggle as the webhook — if AI trading is
  disabled, chat can still answer questions via the Q&A path but a `"trade"` intent is
  rejected with a chat message explaining AI trading is disabled, same as a disabled
  webhook token would 404). Launches `handle_chat_message` as a `BackgroundTasks` job
  with `on_event` wired to this user's SSE queue, returns `{"status": "processing"}`
  immediately (mirrors the webhook's fire-and-return-200 pattern).
- `GET /ai/chat/stream` (Clerk-authed) — `StreamingResponse` with
  `media_type="text/event-stream"`, reads from this user's queue and yields
  `data: {json}\n\n` per event, ending the response when a `{"type": "done"}` event is
  read (client reconnects/opens a new stream for each `POST /ai/chat`).
- `GET /ai/chat/messages?limit=` (Clerk-authed) — returns `store.list_chat_messages`.

## 9. Frontend: `/ai-trading/chat` page

New page, linked from the existing `/ai-trading` settings page (not from the main nav,
to keep nav uncluttered — a "Chat" button/link on the settings page opens it).

- Chat message list (user messages right-aligned, assistant left-aligned, matching any
  existing chat-style UI convention in the app if one exists — otherwise a simple
  bordered list), oldest to newest, loaded via `GET /ai/chat/messages` on mount.
- A message a trade triggered is visually distinguished (e.g. a small badge/icon) from a
  pure Q&A reply, using the message's `signal_id` (non-null = real trade attempt).
- A text input + send button calling `POST /ai/chat`, which immediately opens an
  `EventSource` to `GET /ai/chat/stream` and renders a live "thinking" panel showing
  each streamed event grouped by node (data_agent / coordinator / executer), tokens
  appended as they arrive, tool calls and guardrail results shown as distinct log lines.
  On the stream's `done` event, the panel's content collapses into the final chat
  message bubble and the EventSource closes.
- Reuses `api()`/`ApiError` from `lib/api.ts` for the non-streaming calls; `EventSource`
  is browser-native, no new dependency needed for SSE itself.

## 10. Error handling

- `resolve_chat_intent` failing to parse (malformed LLM JSON) → treated as `"question"`
  intent with the raw message passed through to the Q&A path, so a parsing hiccup never
  accidentally skips straight to a trade attempt — fail toward the safer (non-trading)
  path.
- Any exception during `handle_chat_message` (OpenAI error, Delta error, etc.) is caught
  at the top level, an assistant chat message is added explaining the failure in plain
  terms, a `{"type": "error", "message": ...}` SSE event is emitted, and (if a signal was
  already created) `store.update_signal_result(signal_id, "error", {...})` is called —
  mirrors the webhook path's existing error handling exactly.

## 11. Testing

- Backend:
  - `ai_pipeline.py` streaming changes: tests proving `on_event=None` produces identical
    output to before (re-run of all existing tests, plus a new test asserting a node
    called with no `on_event` never touches any callback) and a new test proving
    `on_event`, when provided, receives the expected token/tool_call/guardrail_result
    events for a mocked streaming `call_llm`.
  - `ai_chat.py`: `resolve_chat_intent` tests (trade message, question message, malformed
    LLM response falls back to question), `handle_chat_message` tests for the Q&A path
    (no `record_incoming_signal` call, no guardrails touched) and the trade path (calls
    `record_incoming_signal`, respects a `duplicate_skipped`/`cooldown_skipped` result by
    not running the pipeline, calls `executer_node` and persists the outcome on success).
  - `store.py`: `add_chat_message`/`list_chat_messages` round-trip tests.
  - `main.py`: `POST /ai/chat` and `GET /ai/chat/messages` endpoint tests; SSE stream
    tested by directly exercising the queue-based event delivery function rather than a
    real long-lived HTTP stream in the test suite (TestClient streaming tests are
    brittle — test the event-formatting/queueing logic as a unit instead).
- Frontend: Vitest tests for any new pure helpers in `lib/` (e.g. an SSE event-line
  parser if one is needed beyond `EventSource`'s native parsing); the page component
  itself follows the existing repo convention of no dedicated page-level test file.

## 12. Explicitly deferred

- Horizontal scaling of the SSE mechanism (Redis pub/sub or similar).
- Editing/deleting past chat messages.
- Rate-limiting the chat endpoint beyond what the existing dedup/cooldown on the
  underlying trade signal already provides.
