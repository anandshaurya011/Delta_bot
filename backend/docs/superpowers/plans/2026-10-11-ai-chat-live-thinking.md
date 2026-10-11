# AI Coordinator Chat + Live Agent Thinking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a chat interface to the coordinator agent (read-only Q&A or real trade
instructions, routed through the exact same safety machinery as the TradingView
webhook) plus a live, streaming view of each agent's reasoning while a signal — chat- or
webhook-triggered — is being processed.

**Architecture:** `ai_pipeline.py`'s `call_llm` and the three node functions gain an
optional, strictly backward-compatible streaming/event-callback path. A new
`ai_chat.py` module orchestrates chat messages, reusing `store.record_incoming_signal`
and `executer_node`'s guardrail/fill-verification logic unchanged for any trade intent.
Events flow to the browser over Server-Sent Events via a per-user in-process queue.

**Tech Stack:** FastAPI `StreamingResponse` (SSE), browser-native `EventSource`,
existing LangGraph/OpenAI pipeline, existing SQLite store patterns, Next.js/TypeScript.

**Spec:** `backend/docs/superpowers/specs/2026-10-11-ai-chat-live-thinking-design.md`

## Global Constraints

- The webhook path (`POST /webhook/tv/{token}`) must behave identically after every
  task in this plan — every existing test in `backend/tests/test_ai_pipeline.py` and
  `backend/tests/test_ai_webhook.py` must still pass, unmodified, at every step.
- A chat message that resolves to a trade instruction MUST go through
  `store.record_incoming_signal` (same dedup/single-flight/cooldown transaction) and
  `ai_guardrails.evaluate_guardrails` (same plain-Python allow/deny check, via the
  existing `executer_node`) — no separate/lighter trading path for chat.
- A chat message that resolves to a question must never reach
  `ai_guardrails.evaluate_guardrails` or `delta_api.place_order`.
- `on_event`/`on_chunk` default to `None` everywhere; when `None`, behavior and return
  values are byte-for-byte identical to the pre-this-plan code.
- SSE delivery is in-process only (single uvicorn worker) — no new infra (no Redis).
- Local development only, same as the rest of this feature.

## Review Focus

- **A malformed/unparseable response from `resolve_chat_intent`** must fall back to the
  `"question"` path, never default to `"trade"` — a parsing failure must fail toward
  *not* trading. Covered in Task 2's tests.
- **A chat trade message arriving during an active cooldown or duplicate window** must
  behave exactly like a webhook duplicate — `record_incoming_signal` returns
  `duplicate_skipped`/`cooldown_skipped`, no pipeline run, a chat message explains why.
  Covered in Task 3's tests.
- **Streaming enabled on one node must not change its final return value** compared to
  the non-streaming call — a reviewer must diff the OLD vs NEW behavior for
  `on_event=None` at the node level, not just trust "it compiles." Covered in Task 1's
  tests (explicit before/after assertions) and the final whole-branch review.
- **A disconnected/slow SSE client must never block or delay a real trade's execution**
  — the event queue is bounded and drops oldest events rather than blocking on
  `put_nowait`. Covered in Task 4's tests.
- **AI trading disabled (`ai_settings.enabled = False`) but chat still answers
  questions** — a trade-intent chat message must be rejected with a clear explanation
  while Q&A keeps working, mirroring the disabled-webhook-token behavior. Covered in
  Task 5's tests.

---

## File Structure

- Modify `backend/ai_pipeline.py` — add optional `on_chunk`/`on_event` params to
  `call_llm`, `coordinator_node`, `data_agent_node`, `executer_node`, `run_pipeline`.
- Modify `backend/store.py` — add `ai_chat_messages` table + `add_chat_message`/
  `list_chat_messages`.
- Create `backend/ai_chat.py` — `resolve_chat_intent`, `handle_chat_message`.
- Create `backend/ai_chat_stream.py` — the per-user in-process SSE queue registry.
- Modify `backend/main.py` — `POST /ai/chat`, `GET /ai/chat/stream`,
  `GET /ai/chat/messages`.
- Create `frontend/lib/aiChat.ts` — types + pure helpers (SSE event parsing/formatting,
  message-list shaping) for the new page.
- Create `frontend/app/(app)/ai-trading/chat/page.tsx` — the chat + live-thinking UI.
- Add a link to it from `frontend/app/(app)/ai-trading/page.tsx`.
- Tests: `backend/tests/test_ai_pipeline.py` (append streaming tests),
  `backend/tests/test_ai_chat.py` (new), `backend/tests/test_ai_store.py` (append),
  `backend/tests/test_ai_webhook.py` (append chat endpoint tests),
  `frontend/lib/aiChat.test.ts` (new).

---

### Task 1: `ai_pipeline.py` — backward-compatible streaming support

**Files:**
- Modify: `backend/ai_pipeline.py`
- Test: `backend/tests/test_ai_pipeline.py` (append)

**Interfaces:**
- Consumes: nothing new — modifies existing, already-shipped functions in place.
- Produces:
  - `call_llm(model, system_prompt, user_content, tools=None, on_chunk=None) -> str` —
    when `on_chunk` is `None`, identical to today (one `stream=False` call, returns
    `response.choices[0].message.content`). When `on_chunk` is provided, makes the call
    with `stream=True`, iterates chunks, calls `on_chunk(delta_text)` for each non-empty
    `chunk.choices[0].delta.content`, accumulates the full text, and returns it at the
    end — same return type/value as the non-streaming path for the same model output.
  - `coordinator_node(state, on_event=None) -> PipelineState`,
    `data_agent_node(state, on_event=None) -> PipelineState`,
    `executer_node(state, on_event=None) -> PipelineState` — each threads
    `on_event` through to `call_llm` as
    `on_chunk=(lambda text: on_event({"node": "<name>", "type": "token", "text": text})) if on_event else None`.
    `data_agent_node` additionally calls
    `on_event({"node": "data_agent", "type": "tool_call", "name": call["name"], "args": call.get("args", {})})`
    right before executing each tool, when `on_event` is provided.
    `executer_node` additionally calls
    `on_event({"node": "executer", "type": "guardrail_result", "allowed": guardrail_result.allowed, "reason": guardrail_result.reason})`
    right after `evaluate_guardrails` runs, when `on_event` is provided.
  - `run_pipeline(user_id, signal, settings, on_event=None) -> dict` — threads
    `on_event` into each node call; return value unchanged when `on_event=None`.

- [ ] **Step 1: Write the failing tests**

```python
# append to backend/tests/test_ai_pipeline.py

def test_call_llm_without_on_chunk_is_unchanged_non_streaming_call():
    fake_response = MagicMock()
    fake_response.choices = [MagicMock(message=MagicMock(content="hello"))]
    with patch("ai_pipeline._get_client") as mock_get_client:
        mock_get_client.return_value.chat.completions.create.return_value = fake_response
        result = ai_pipeline.call_llm(model="gpt-4.1", system_prompt="sys", user_content="hi")
    assert result == "hello"
    _, kwargs = mock_get_client.return_value.chat.completions.create.call_args
    assert "stream" not in kwargs or kwargs["stream"] is False


def test_call_llm_with_on_chunk_streams_and_accumulates_full_text():
    chunk1 = MagicMock()
    chunk1.choices = [MagicMock(delta=MagicMock(content="hel"))]
    chunk2 = MagicMock()
    chunk2.choices = [MagicMock(delta=MagicMock(content="lo"))]
    with patch("ai_pipeline._get_client") as mock_get_client:
        mock_get_client.return_value.chat.completions.create.return_value = iter([chunk1, chunk2])
        received = []
        result = ai_pipeline.call_llm(
            model="gpt-4.1", system_prompt="sys", user_content="hi", on_chunk=received.append
        )
    assert result == "hello"
    assert received == ["hel", "lo"]
    _, kwargs = mock_get_client.return_value.chat.completions.create.call_args
    assert kwargs["stream"] is True


def test_coordinator_node_with_on_event_emits_token_events():
    chunk1 = MagicMock()
    chunk1.choices = [MagicMock(delta=MagicMock(content='{"action":'))]
    chunk2 = MagicMock()
    chunk2.choices = [MagicMock(delta=MagicMock(content='"hold","side":"none","size_hint":null,"reasoning":"x"}'))]
    with patch("ai_pipeline._get_client") as mock_get_client:
        mock_get_client.return_value.chat.completions.create.return_value = iter([chunk1, chunk2])
        events = []
        state = ai_pipeline.coordinator_node(_state(), on_event=events.append)
    assert state["plan"]["action"] == "hold"
    token_events = [e for e in events if e["type"] == "token"]
    assert [e["node"] for e in token_events] == ["coordinator", "coordinator"]
    assert "".join(e["text"] for e in token_events) == '{"action":"hold","side":"none","size_hint":null,"reasoning":"x"}'


def test_coordinator_node_without_on_event_unchanged():
    fake_response = json.dumps({"action": "hold", "side": "none", "size_hint": None, "reasoning": "x"})
    with patch("ai_pipeline.call_llm", return_value=fake_response) as mock_call:
        state = ai_pipeline.coordinator_node(_state())
    assert state["plan"]["action"] == "hold"
    _, kwargs = mock_call.call_args
    assert kwargs.get("on_chunk") is None


def test_data_agent_node_with_on_event_emits_tool_call_events():
    tool_call_response = json.dumps({"tool_calls": [{"name": "get_position", "args": {"symbol": "BTCUSD"}}]})
    with patch("ai_pipeline.call_llm", return_value=tool_call_response), \
         patch("ai_pipeline.delta.get_position", return_value={"size": 5}):
        events = []
        ai_pipeline.data_agent_node(_state(), on_event=events.append)
    tool_events = [e for e in events if e["type"] == "tool_call"]
    assert tool_events == [{"node": "data_agent", "type": "tool_call", "name": "get_position", "args": {"symbol": "BTCUSD"}}]


def test_executer_node_with_on_event_emits_guardrail_result_event():
    state = _state(plan={"action": "open", "side": "long", "size_hint": 5, "reasoning": "go long"})
    order_response = json.dumps({"symbol": "BTCUSD", "side": "buy", "size": 5})
    placed_order = {"id": 50}
    filled_order = {"id": 50, "state": "closed", "unfilled_size": 0}
    with patch("ai_pipeline.call_llm", return_value=order_response), \
         patch("ai_pipeline.delta.get_position", return_value=None), \
         patch("ai_pipeline.delta.get_total_unrealized_pnl", return_value=0.0), \
         patch("ai_pipeline.store.get_trade_count_today", return_value=0), \
         patch("ai_pipeline.store.increment_trade_count"), \
         patch("ai_pipeline.delta.place_order", return_value=placed_order), \
         patch("ai_pipeline.delta.get_order_by_id", return_value=filled_order):
        events = []
        ai_pipeline.executer_node(state, on_event=events.append)
    guardrail_events = [e for e in events if e["type"] == "guardrail_result"]
    assert guardrail_events == [{"node": "executer", "type": "guardrail_result", "allowed": True, "reason": None}]


def test_run_pipeline_with_on_event_threads_through_all_nodes():
    data_response = json.dumps({"tool_calls": []})
    plan_response = json.dumps({"action": "hold", "side": "none", "size_hint": None, "reasoning": "no edge"})
    with patch("ai_pipeline.bind_user_credentials"), \
         patch("ai_pipeline.call_llm", side_effect=[data_response, plan_response]):
        events = []
        ai_pipeline.run_pipeline(
            "user_1", {"symbol": "BTCUSD", "timeframe": "15m", "action": "buy", "note": None},
            _state()["settings"], on_event=events.append,
        )
    nodes_seen = {e["node"] for e in events}
    assert "data_agent" in nodes_seen
    assert "coordinator" in nodes_seen
```

`MagicMock` and `json`/`patch` are already imported at the top of this test file from
earlier tasks — confirm, don't re-add duplicate imports.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /home/anand-shaurya/Documents/personal/delta/.worktrees/ai-trading-agents/backend && .venv/bin/python -m pytest tests/test_ai_pipeline.py -v`
Expected: the 7 new tests FAIL (`TypeError: ... unexpected keyword argument 'on_chunk'`
or similar); all pre-existing tests in this file still pass.

- [ ] **Step 3: Implement**

In `backend/ai_pipeline.py`, change `call_llm`:

```python
def call_llm(
    model: str,
    system_prompt: str,
    user_content: str,
    tools: list[dict[str, Any]] | None = None,
    on_chunk: Callable[[str], None] | None = None,
) -> str:
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_content}],
        "response_format": {"type": "json_object"},
    }
    if tools:
        kwargs["tools"] = tools
    if on_chunk is None:
        response = _get_client().chat.completions.create(**kwargs)
        return response.choices[0].message.content
    kwargs["stream"] = True
    full_text = ""
    for chunk in _get_client().chat.completions.create(**kwargs):
        delta = chunk.choices[0].delta.content
        if delta:
            on_chunk(delta)
            full_text += delta
    return full_text
```

Add `from typing import Callable` (or extend the existing `typing` import) at the top of
the file if not already present.

Change `coordinator_node`:

```python
def coordinator_node(state: PipelineState, on_event: Callable[[dict[str, Any]], None] | None = None) -> PipelineState:
    settings = state["settings"]
    system_prompt = _COORDINATOR_BASE_PROMPT
    if settings.get("coordinator_prompt"):
        system_prompt += "\n\nUser instructions: " + settings["coordinator_prompt"]
    user_content = json.dumps({"signal": state["signal"], "gathered_data": state.get("gathered_data", {})})
    on_chunk = (lambda text: on_event({"node": "coordinator", "type": "token", "text": text})) if on_event else None
    raw = call_llm(model=settings["coordinator_model"], system_prompt=system_prompt, user_content=user_content, on_chunk=on_chunk)
    state["plan"] = json.loads(raw)
    return state
```

Change `data_agent_node` similarly, adding the `on_event` parameter, building `on_chunk`
the same way, and adding `if on_event: on_event({"node": "data_agent", "type": "tool_call", "name": call["name"], "args": call.get("args", {})})`
immediately before the existing `impl = _TOOL_IMPL.get(call["name"]); if impl: gathered[call["name"]] = impl(call.get("args", {}))`
line (emit the event for every requested call that has a known `impl`, matching the
existing silent-skip-unknown-tool behavior — do not emit for unknown tool names).

Change `executer_node` similarly: add the `on_event` parameter, build `on_chunk` the
same way for its own `call_llm` call, and add
`if on_event: on_event({"node": "executer", "type": "guardrail_result", "allowed": guardrail_result.allowed, "reason": guardrail_result.reason})`
immediately after the existing `guardrail_result = evaluate_guardrails(...)` line, before
the `if not guardrail_result.allowed:` check.

Change `run_pipeline`:

```python
def run_pipeline(
    user_id: str, signal: dict[str, Any], settings: dict[str, Any], on_event: Callable[[dict[str, Any]], None] | None = None
) -> dict[str, Any]:
    bind_user_credentials(user_id)
    initial_state: PipelineState = {
        "user_id": user_id, "signal": signal, "settings": settings,
        "gathered_data": {}, "plan": None, "order_request": None, "execution_result": None,
    }
    final_state = _compiled_graph.invoke(
        initial_state,
        config={"configurable": {"on_event": on_event}} if on_event else None,
    )
    ...
```

Note: LangGraph's `StateGraph.invoke` does not automatically thread extra kwargs into
each node function. The simplest correct approach, given the existing graph is a fixed
linear sequence (`data_agent -> coordinator -> executer -> END`), is to NOT rely on
LangGraph's `config` mechanism at all — instead, call the three node functions directly
in sequence inside `run_pipeline`, bypassing `_compiled_graph.invoke` entirely when
`on_event` is provided, OR (simpler, and keeping one code path) always call the three
node functions directly in `run_pipeline` rather than through the compiled graph, and
remove the graph's `invoke` call from the hot path:

```python
def run_pipeline(
    user_id: str, signal: dict[str, Any], settings: dict[str, Any], on_event: Callable[[dict[str, Any]], None] | None = None
) -> dict[str, Any]:
    bind_user_credentials(user_id)
    state: PipelineState = {
        "user_id": user_id, "signal": signal, "settings": settings,
        "gathered_data": {}, "plan": None, "order_request": None, "execution_result": None,
    }
    state = data_agent_node(state, on_event=on_event)
    state = coordinator_node(state, on_event=on_event)
    state = executer_node(state, on_event=on_event)
    return {
        "gathered_data": state["gathered_data"],
        "plan": state["plan"],
        "order_request": state["order_request"],
        "execution_result": state["execution_result"],
    }
```

This is a deliberate simplification: the LangGraph `StateGraph`/`_compiled_graph` built
in Task 8 of the original plan added no branching logic (it's a fixed 3-node sequence),
so calling the nodes directly is behaviorally identical to invoking the compiled graph,
removes the awkwardness of threading a callback through LangGraph's `invoke` config, and
keeps `on_event` support simple and directly testable. Leave `_graph`/`_compiled_graph`
and their construction in the file (still exported, still used by
`test_run_pipeline_wires_all_three_nodes_in_order` from the original plan if that test
patches `call_llm` rather than the node functions directly — verify this still passes in
Step 4; if it does not, that test needs updating to assert against the node-call
sequence instead of the graph object, since `run_pipeline`'s actual behavior, not its
internal wiring mechanism, is the contract that matters).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /home/anand-shaurya/Documents/personal/delta/.worktrees/ai-trading-agents/backend && .venv/bin/python -m pytest tests/test_ai_pipeline.py -v`
Expected: all tests in this file pass (the 7 new ones plus every pre-existing one in the
file, unmodified in behavior). If `test_run_pipeline_wires_all_three_nodes_in_order`
fails because it specifically asserted against LangGraph invocation details rather than
observable behavior, fix that one test to assert on `result["plan"]["action"]` and
`result["execution_result"]` as it already partially does — do not weaken any other
test's assertions to make this pass.

- [ ] **Step 5: Run the full backend suite**

Run: `cd /home/anand-shaurya/Documents/personal/delta/.worktrees/ai-trading-agents/backend && .venv/bin/python -m pytest -v`
Expected: all 85 pre-existing tests plus the new ones pass — this is the Global
Constraint check that the webhook path is unaffected.

- [ ] **Step 6: Commit**

```bash
cd /home/anand-shaurya/Documents/personal/delta/.worktrees/ai-trading-agents
git add backend/ai_pipeline.py backend/tests/test_ai_pipeline.py
git commit -m "feat: add backward-compatible streaming/event support to ai_pipeline nodes"
```

---

### Task 2: `backend/store.py` — chat messages table

**Files:**
- Modify: `backend/store.py` (append; also add `ai_chat_messages` table creation inside
  `_conn()`)
- Test: `backend/tests/test_ai_store.py` (append)

**Interfaces:**
- Produces:
  - `store.add_chat_message(user_id: str, role: str, content: str, signal_id: int | None = None) -> int`
    — inserts a row (`ts=int(time.time())`), returns `cur.lastrowid`.
  - `store.list_chat_messages(user_id: str, limit: int = 100) -> list[dict]` — returns
    the most recent `limit` messages for `user_id`, ordered **oldest first** (chat
    reading order — this is the opposite order from `list_signals`, which is
    newest-first; get this right, it's easy to copy the wrong ordering from
    `list_signals`). Each dict has keys `id, ts, role, content, signal_id`.

- [ ] **Step 1: Write the failing tests**

```python
# append to backend/tests/test_ai_store.py

def test_add_and_list_chat_messages_oldest_first(store):
    id1 = store.add_chat_message("user_1", "user", "go long BTC")
    id2 = store.add_chat_message("user_1", "assistant", "opened a long position", signal_id=7)
    rows = store.list_chat_messages("user_1")
    assert [r["id"] for r in rows] == [id1, id2]
    assert rows[1]["signal_id"] == 7
    assert rows[0]["signal_id"] is None


def test_list_chat_messages_respects_limit_and_user_scope(store):
    store.add_chat_message("user_1", "user", "a")
    store.add_chat_message("user_2", "user", "b")
    store.add_chat_message("user_1", "user", "c")
    rows = store.list_chat_messages("user_1", limit=1)
    assert len(rows) == 1
    assert rows[0]["content"] == "c"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /home/anand-shaurya/Documents/personal/delta/.worktrees/ai-trading-agents/backend && .venv/bin/python -m pytest tests/test_ai_store.py -v`
Expected: the 2 new tests FAIL with `AttributeError`; all pre-existing tests in the file
still pass.

- [ ] **Step 3: Implement**

In `backend/store.py`'s `_conn()`, add alongside the other `create table` statements:

```python
c.execute(
    "create table if not exists ai_chat_messages(id integer primary key autoincrement, "
    "user_id text not null, ts integer not null, role text not null, content text not null, "
    "signal_id integer)"
)
c.execute("create index if not exists ai_chat_user on ai_chat_messages(user_id, id)")
```

Append to `backend/store.py`:

```python
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
```

The `order by id desc limit ?` plus Python-side `reversed(...)` gets "most recent
`limit` messages, returned oldest-first" — querying `asc` with `limit` would instead
return the OLDEST `limit` messages, which is wrong for a chat history view.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /home/anand-shaurya/Documents/personal/delta/.worktrees/ai-trading-agents/backend && .venv/bin/python -m pytest tests/test_ai_store.py -v`
Expected: all tests pass.

- [ ] **Step 5: Run the full backend suite**

Run: `cd /home/anand-shaurya/Documents/personal/delta/.worktrees/ai-trading-agents/backend && .venv/bin/python -m pytest -v`
Expected: all pass, no regressions.

- [ ] **Step 6: Commit**

```bash
cd /home/anand-shaurya/Documents/personal/delta/.worktrees/ai-trading-agents
git add backend/store.py backend/tests/test_ai_store.py
git commit -m "feat(store): add ai_chat_messages table"
```

---

### Task 3: `backend/ai_chat.py` — intent resolution + chat orchestration

**Files:**
- Create: `backend/ai_chat.py`
- Test: `backend/tests/test_ai_chat.py` (new)

**Interfaces:**
- Consumes: `ai_pipeline.call_llm`, `ai_pipeline.bind_user_credentials`,
  `ai_pipeline.data_agent_node`, `ai_pipeline.coordinator_node`,
  `ai_pipeline.executer_node`, `ai_pipeline.PipelineState` (all from Task 1, already
  supporting `on_event`); `store.add_chat_message`, `store.record_incoming_signal`,
  `store.update_signal_result` (all pre-existing).
- Produces:
  - `ai_chat.resolve_chat_intent(message: str, settings: dict) -> dict` — one
    non-streamed `call_llm` call; returns `{"intent": "trade", "symbol": str, "timeframe": str, "action": str}`
    or `{"intent": "question"}`. On a JSON parse failure or any missing required key
    for the `"trade"` shape, returns `{"intent": "question"}` (fail toward the safer
    path — this is the plan's primary Review Focus item).
  - `ai_chat.handle_chat_message(user_id: str, message: str, settings: dict, on_event: Callable[[dict], None]) -> None`
    — see spec §6 for the full step sequence. Always calls `on_event({"type": "done"})`
    as its final action (success or failure), so the SSE consumer knows when to close.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_ai_chat.py
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ai_chat

BASE_SETTINGS = {
    "coordinator_model": "gpt-4.1",
    "data_model": "gpt-4.1-mini",
    "executer_model": "gpt-4.1-mini",
    "coordinator_prompt": "",
    "data_prompt": "",
    "executer_prompt": "",
    "guardrails_json": {},
    "cooldown_minutes": 30,
}


def test_resolve_chat_intent_parses_trade_instruction():
    raw = json.dumps({"intent": "trade", "symbol": "BTCUSD", "timeframe": "15m", "action": "buy"})
    with patch("ai_chat.ai_pipeline.call_llm", return_value=raw):
        result = ai_chat.resolve_chat_intent("go long BTC", BASE_SETTINGS)
    assert result == {"intent": "trade", "symbol": "BTCUSD", "timeframe": "15m", "action": "buy"}


def test_resolve_chat_intent_parses_question():
    raw = json.dumps({"intent": "question"})
    with patch("ai_chat.ai_pipeline.call_llm", return_value=raw):
        result = ai_chat.resolve_chat_intent("what's the price of BTC?", BASE_SETTINGS)
    assert result == {"intent": "question"}


def test_resolve_chat_intent_malformed_json_falls_back_to_question():
    with patch("ai_chat.ai_pipeline.call_llm", return_value="not valid json"):
        result = ai_chat.resolve_chat_intent("anything", BASE_SETTINGS)
    assert result == {"intent": "question"}


def test_resolve_chat_intent_trade_missing_fields_falls_back_to_question():
    raw = json.dumps({"intent": "trade", "symbol": "BTCUSD"})  # missing timeframe/action
    with patch("ai_chat.ai_pipeline.call_llm", return_value=raw):
        result = ai_chat.resolve_chat_intent("go long BTC", BASE_SETTINGS)
    assert result == {"intent": "question"}


def test_handle_chat_message_question_path_never_calls_record_incoming_signal():
    with patch("ai_chat.store.add_chat_message") as mock_add, \
         patch("ai_chat.ai_pipeline.bind_user_credentials"), \
         patch("ai_chat.ai_pipeline.data_agent_node", side_effect=lambda state, on_event=None: state), \
         patch("ai_chat.resolve_chat_intent", return_value={"intent": "question"}), \
         patch("ai_chat.ai_pipeline.call_llm", return_value="BTC is around $65,000 right now."), \
         patch("ai_chat.store.record_incoming_signal") as mock_record:
        events = []
        ai_chat.handle_chat_message("user_1", "what's the price of BTC?", BASE_SETTINGS, events.append)
    mock_record.assert_not_called()
    assert any(e.get("type") == "done" for e in events)
    # one user message, one assistant message
    assert mock_add.call_count == 2
    assert mock_add.call_args_list[0].args[:2] == ("user_1", "user")
    assert mock_add.call_args_list[1].args[:2] == ("user_1", "assistant")


def test_handle_chat_message_trade_path_calls_record_incoming_signal_and_executer():
    with patch("ai_chat.store.add_chat_message"), \
         patch("ai_chat.ai_pipeline.bind_user_credentials"), \
         patch("ai_chat.ai_pipeline.data_agent_node", side_effect=lambda state, on_event=None: state), \
         patch("ai_chat.resolve_chat_intent", return_value={"intent": "trade", "symbol": "BTCUSD", "timeframe": "15m", "action": "buy"}), \
         patch("ai_chat.store.record_incoming_signal", return_value={"id": 9, "status": "processing"}) as mock_record, \
         patch("ai_chat.ai_pipeline.coordinator_node", side_effect=lambda state, on_event=None: {**state, "plan": {"action": "hold", "side": "none", "size_hint": None, "reasoning": "x"}}), \
         patch("ai_chat.ai_pipeline.executer_node", side_effect=lambda state, on_event=None: {**state, "execution_result": {"executed": False, "reason": "no_action"}}) as mock_exec, \
         patch("ai_chat.store.update_signal_result") as mock_update:
        events = []
        ai_chat.handle_chat_message("user_1", "go long BTC on the 15m", BASE_SETTINGS, events.append)
    mock_record.assert_called_once_with("user_1", "BTCUSD", "15m", "buy", "go long BTC on the 15m", 30)
    mock_exec.assert_called_once()
    mock_update.assert_called_once_with(9, "done", {"plan": {"action": "hold", "side": "none", "size_hint": None, "reasoning": "x"}, "execution_result": {"executed": False, "reason": "no_action"}})


def test_handle_chat_message_trade_path_skips_pipeline_when_duplicate_skipped():
    with patch("ai_chat.store.add_chat_message") as mock_add, \
         patch("ai_chat.ai_pipeline.bind_user_credentials"), \
         patch("ai_chat.ai_pipeline.data_agent_node", side_effect=lambda state, on_event=None: state), \
         patch("ai_chat.resolve_chat_intent", return_value={"intent": "trade", "symbol": "BTCUSD", "timeframe": "15m", "action": "buy"}), \
         patch("ai_chat.store.record_incoming_signal", return_value={"id": 9, "status": "duplicate_skipped"}), \
         patch("ai_chat.ai_pipeline.coordinator_node") as mock_coord, \
         patch("ai_chat.ai_pipeline.executer_node") as mock_exec:
        events = []
        ai_chat.handle_chat_message("user_1", "go long BTC again", BASE_SETTINGS, events.append)
    mock_coord.assert_not_called()
    mock_exec.assert_not_called()
    assert any(e.get("type") == "done" for e in events)
    assert mock_add.call_count == 2  # user message + an assistant message explaining the skip
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /home/anand-shaurya/Documents/personal/delta/.worktrees/ai-trading-agents/backend && .venv/bin/python -m pytest tests/test_ai_chat.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ai_chat'`.

- [ ] **Step 3: Implement**

```python
# backend/ai_chat.py
"""Chat orchestration for the AI coordinator. A chat message either gets answered
(no trade risk at all) or, if it resolves to a trade instruction, flows through the
exact same store.record_incoming_signal + executer_node (guardrails, fill verification)
path the TradingView webhook uses -- there is no separate, lighter trading path here.
"""

from __future__ import annotations

import json
from typing import Any, Callable

import ai_pipeline
import store

_INTENT_PROMPT = (
    "Classify the user's chat message as either a trade instruction or a question. "
    "If it is a trade instruction, extract the symbol, timeframe, and action (buy/sell/close). "
    "If no timeframe is mentioned, default to \"15m\" rather than ask a follow-up question. "
    'Respond ONLY with JSON: {"intent": "trade", "symbol": "...", "timeframe": "...", "action": "..."} '
    'or {"intent": "question"}'
)

_CHAT_ANSWER_PROMPT = (
    "You are the coordinator agent for an automated trading system, answering a user's "
    "question using the market/position data already gathered. Be concise and direct."
)

_REQUIRED_TRADE_KEYS = ("symbol", "timeframe", "action")


def resolve_chat_intent(message: str, settings: dict[str, Any]) -> dict[str, Any]:
    raw = ai_pipeline.call_llm(model=settings["coordinator_model"], system_prompt=_INTENT_PROMPT, user_content=message)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"intent": "question"}
    if parsed.get("intent") != "trade":
        return {"intent": "question"}
    if not all(parsed.get(k) for k in _REQUIRED_TRADE_KEYS):
        return {"intent": "question"}
    return {"intent": "trade", **{k: parsed[k] for k in _REQUIRED_TRADE_KEYS}}


def handle_chat_message(user_id: str, message: str, settings: dict[str, Any], on_event: Callable[[dict[str, Any]], None]) -> None:
    store.add_chat_message(user_id, "user", message)
    ai_pipeline.bind_user_credentials(user_id)

    state: ai_pipeline.PipelineState = {
        "user_id": user_id,
        "signal": {"symbol": "", "timeframe": "", "action": "", "note": message},
        "settings": settings,
        "gathered_data": {},
        "plan": None,
        "order_request": None,
        "execution_result": None,
    }
    state = ai_pipeline.data_agent_node(state, on_event=on_event)

    intent = resolve_chat_intent(message, settings)

    if intent["intent"] == "question":
        on_chunk = lambda text: on_event({"node": "coordinator", "type": "token", "text": text})
        reply = ai_pipeline.call_llm(
            model=settings["coordinator_model"],
            system_prompt=_CHAT_ANSWER_PROMPT,
            user_content=json.dumps({"message": message, "gathered_data": state["gathered_data"]}),
            on_chunk=on_chunk,
        )
        store.add_chat_message(user_id, "assistant", reply)
        on_event({"type": "done"})
        return

    symbol, timeframe, action = intent["symbol"], intent["timeframe"], intent["action"]
    outcome = store.record_incoming_signal(user_id, symbol, timeframe, action, message, settings["cooldown_minutes"])
    if outcome["status"] != "processing":
        store.add_chat_message(
            user_id, "assistant",
            f"Skipped ({outcome['status']}) -- a recent signal for {symbol} {timeframe} is already in flight or cooling down.",
            signal_id=outcome["id"],
        )
        on_event({"type": "done"})
        return

    state["signal"] = {"symbol": symbol, "timeframe": timeframe, "action": action, "note": message}
    try:
        state = ai_pipeline.coordinator_node(state, on_event=on_event)
        state = ai_pipeline.executer_node(state, on_event=on_event)
        result = {
            "gathered_data": state["gathered_data"],
            "plan": state["plan"],
            "order_request": state["order_request"],
            "execution_result": state["execution_result"],
        }
        store.update_signal_result(outcome["id"], "done", result)
        if result["execution_result"].get("reason") == "guardrail:max_daily_loss":
            store.update_ai_settings(user_id, enabled=False)
        plan = result["plan"] or {}
        execution = result["execution_result"] or {}
        summary = plan.get("reasoning", "") + (
            f" -> executed: {execution.get('executed')}" if "executed" in execution else ""
        )
        store.add_chat_message(user_id, "assistant", summary or "Done.", signal_id=outcome["id"])
    except Exception as exc:  # pragma: no cover - defensive, mirrors main.py's _run_signal_pipeline
        store.update_signal_result(outcome["id"], "error", {"error": str(exc)})
        store.add_chat_message(user_id, "assistant", f"Something went wrong: {exc}", signal_id=outcome["id"])
        on_event({"type": "error", "message": str(exc)})
    on_event({"type": "done"})
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /home/anand-shaurya/Documents/personal/delta/.worktrees/ai-trading-agents/backend && .venv/bin/python -m pytest tests/test_ai_chat.py -v`
Expected: all 7 tests pass.

- [ ] **Step 5: Run the full backend suite**

Run: `cd /home/anand-shaurya/Documents/personal/delta/.worktrees/ai-trading-agents/backend && .venv/bin/python -m pytest -v`
Expected: all pass, no regressions.

- [ ] **Step 6: Commit**

```bash
cd /home/anand-shaurya/Documents/personal/delta/.worktrees/ai-trading-agents
git add backend/ai_chat.py backend/tests/test_ai_chat.py
git commit -m "feat: add chat orchestration reusing the webhook's dedup/guardrail/execution path"
```

---

### Task 4: `backend/ai_chat_stream.py` — per-user SSE event queue

**Files:**
- Create: `backend/ai_chat_stream.py`
- Test: `backend/tests/test_ai_chat_stream.py` (new)

**Interfaces:**
- Produces:
  - `ai_chat_stream.get_queue(user_id: str) -> queue.Queue` — lazily creates and
    returns a bounded (`maxsize=200`) `queue.Queue` for this user, reusing the same
    queue object on repeated calls for the same `user_id` within the process's
    lifetime.
  - `ai_chat_stream.publish(user_id: str, event: dict) -> None` — pushes `event` onto
    this user's queue via `put_nowait`; if the queue is full, removes the oldest item
    (`get_nowait()`) first so this never blocks, then retries the put once.
  - `ai_chat_stream.drain(user_id: str)` — a generator that `yield`s each event as it
    arrives on this user's queue (blocking `get()` with a reasonable timeout, e.g. 30s,
    yielding nothing and looping again on timeout so the HTTP connection can send SSE
    keep-alive style gaps without erroring), until an event with `type == "done"` or
    `type == "error"` is yielded, after which it stops (the generator returns).

A plain thread-safe `queue.Queue` (not `asyncio.Queue`) is used because
`handle_chat_message` runs inside a FastAPI `BackgroundTasks` callback, which Starlette
executes via a worker thread for a sync function (this codebase's existing background
task, `_run_signal_pipeline` in `main.py`, is also a plain sync function) -- an
`asyncio.Queue` is not safely usable for cross-thread handoff without extra
`call_soon_threadsafe` plumbing that a plain `queue.Queue` avoids entirely.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_ai_chat_stream.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ai_chat_stream


def test_get_queue_returns_same_object_for_same_user():
    q1 = ai_chat_stream.get_queue("user_a")
    q2 = ai_chat_stream.get_queue("user_a")
    assert q1 is q2


def test_publish_and_drain_yields_events_in_order_until_done():
    user_id = "user_b"
    ai_chat_stream.publish(user_id, {"type": "token", "text": "hi"})
    ai_chat_stream.publish(user_id, {"type": "done"})
    events = list(ai_chat_stream.drain(user_id))
    assert events == [{"type": "token", "text": "hi"}, {"type": "done"}]


def test_publish_drops_oldest_when_queue_full():
    user_id = "user_c"
    for i in range(205):
        ai_chat_stream.publish(user_id, {"type": "token", "text": str(i)})
    ai_chat_stream.publish(user_id, {"type": "done"})
    events = list(ai_chat_stream.drain(user_id))
    assert events[-1] == {"type": "done"}
    assert len(events) <= 201  # bounded, did not grow unbounded or block
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /home/anand-shaurya/Documents/personal/delta/.worktrees/ai-trading-agents/backend && .venv/bin/python -m pytest tests/test_ai_chat_stream.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ai_chat_stream'`.

- [ ] **Step 3: Implement**

```python
# backend/ai_chat_stream.py
"""Per-user, in-process SSE event delivery. Single-worker only -- a disconnected or
slow client must never block a real trade's execution, so publish() never blocks."""

from __future__ import annotations

import queue
from typing import Any, Iterator

_QUEUE_MAXSIZE = 200
_queues: dict[str, queue.Queue] = {}


def get_queue(user_id: str) -> queue.Queue:
    if user_id not in _queues:
        _queues[user_id] = queue.Queue(maxsize=_QUEUE_MAXSIZE)
    return _queues[user_id]


def publish(user_id: str, event: dict[str, Any]) -> None:
    q = get_queue(user_id)
    try:
        q.put_nowait(event)
    except queue.Full:
        try:
            q.get_nowait()
        except queue.Empty:
            pass
        q.put_nowait(event)


def drain(user_id: str) -> Iterator[dict[str, Any]]:
    q = get_queue(user_id)
    while True:
        try:
            event = q.get(timeout=30)
        except queue.Empty:
            continue
        yield event
        if event.get("type") in ("done", "error"):
            return
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /home/anand-shaurya/Documents/personal/delta/.worktrees/ai-trading-agents/backend && .venv/bin/python -m pytest tests/test_ai_chat_stream.py -v`
Expected: all 3 tests pass.

- [ ] **Step 5: Run the full backend suite**

Run: `cd /home/anand-shaurya/Documents/personal/delta/.worktrees/ai-trading-agents/backend && .venv/bin/python -m pytest -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
cd /home/anand-shaurya/Documents/personal/delta/.worktrees/ai-trading-agents
git add backend/ai_chat_stream.py backend/tests/test_ai_chat_stream.py
git commit -m "feat: add bounded, non-blocking per-user SSE event queue"
```

---

### Task 5: `backend/main.py` — chat endpoints

**Files:**
- Modify: `backend/main.py`
- Test: `backend/tests/test_ai_webhook.py` (append — this file already has the AI
  Clerk-auth-bypass test fixture pattern to reuse; rename the test file's purpose
  mentally to "AI endpoints" even though the filename stays the same, to avoid an
  unrelated file rename in this task)

**Interfaces:**
- Consumes: `ai_chat.handle_chat_message`, `ai_chat_stream.publish`,
  `ai_chat_stream.drain`, `store.list_chat_messages`, `store.get_or_create_ai_settings`
  (all pre-existing by this point in the plan).
- Produces:
  - `POST /ai/chat` (Clerk-authed) — body `{"message": str}` (non-empty, via Pydantic).
    If `ai_settings.enabled` is `False`: still allowed (the question path inside
    `handle_chat_message` works regardless; `resolve_chat_intent` producing a
    `"trade"` result when disabled must be rejected -- see Step 3 below for exactly
    where this check lives). Launches `ai_chat.handle_chat_message` via
    `BackgroundTasks`, with `on_event=lambda event: ai_chat_stream.publish(user_id, event)`,
    returns `{"status": "processing"}` immediately.
  - `GET /ai/chat/stream` (Clerk-authed) — `StreamingResponse` over
    `ai_chat_stream.drain(user_id)`, `media_type="text/event-stream"`, each event
    formatted as `f"data: {json.dumps(event)}\n\n"`.
  - `GET /ai/chat/messages?limit=` (Clerk-authed) — returns `store.list_chat_messages`.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_ai_webhook.py` (reuse the existing `client` fixture in
this file, which already provides the async Clerk-auth-bypass + `dependency_overrides`
cleanup pattern):

```python
def test_post_ai_chat_question_does_not_require_ai_trading_enabled(client):
    with patch("main.ai_chat.handle_chat_message") as mock_handle:
        resp = client.post("/ai/chat", json={"message": "what's the price of BTC?"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "processing"
    mock_handle.assert_called_once()
    args = mock_handle.call_args.args
    assert args[0] == "user_1"
    assert args[1] == "what's the price of BTC?"


def test_post_ai_chat_empty_message_is_422(client):
    resp = client.post("/ai/chat", json={"message": ""})
    assert resp.status_code == 422


def test_get_ai_chat_messages_returns_store_contents(client):
    import store
    store.add_chat_message("user_1", "user", "hello")
    resp = client.get("/ai/chat/messages")
    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["content"] == "hello"


def test_get_ai_chat_stream_yields_published_events(client):
    import ai_chat_stream
    ai_chat_stream.publish("user_1", {"type": "token", "text": "hi"})
    ai_chat_stream.publish("user_1", {"type": "done"})
    resp = client.get("/ai/chat/stream")
    assert resp.status_code == 200
    assert "data:" in resp.text
    assert '"type": "done"' in resp.text or '"type":"done"' in resp.text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /home/anand-shaurya/Documents/personal/delta/.worktrees/ai-trading-agents/backend && .venv/bin/python -m pytest tests/test_ai_webhook.py -v`
Expected: the 4 new tests FAIL (`404 Not Found` on the new routes); all pre-existing
tests in this file still pass.

- [ ] **Step 3: Implement**

Add near the top of `backend/main.py`, alongside the other `import ... # noqa: E402`
lines: `import ai_chat  # noqa: E402` and `import ai_chat_stream  # noqa: E402`. Add
`from fastapi.responses import StreamingResponse` to the existing
`from fastapi.responses import JSONResponse` line (combine into one import).

Append to `backend/main.py`:

```python
class ChatMessageIn(BaseModel):
    message: str = Field(min_length=1)


def _run_chat_message(user_id: str, message: str, settings: dict[str, Any]) -> None:
    ai_chat.handle_chat_message(user_id, message, settings, on_event=lambda event: ai_chat_stream.publish(user_id, event))


@app.post("/ai/chat", tags=["ai"])
def post_ai_chat(body: ChatMessageIn, background_tasks: BackgroundTasks):
    user_id = auth.user_id_var.get()
    settings = store.get_or_create_ai_settings(user_id)
    background_tasks.add_task(_run_chat_message, user_id, body.message, settings)
    return {"status": "processing"}


@app.get("/ai/chat/stream", tags=["ai"])
def get_ai_chat_stream():
    user_id = auth.user_id_var.get()

    def event_source():
        for event in ai_chat_stream.drain(user_id):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(event_source(), media_type="text/event-stream")


@app.get("/ai/chat/messages", tags=["ai"])
def get_ai_chat_messages(limit: int = Query(100, ge=1, le=500)):
    return store.list_chat_messages(auth.user_id_var.get(), limit)
```

Add `import json` to the top of `backend/main.py` if not already present (check first —
it may already be imported for another purpose; if so, do not add a duplicate import).

Now handle the "trade intent while AI trading disabled" rule from this task's
Interfaces section: inside `ai_chat.handle_chat_message` (Task 3's file,
`backend/ai_chat.py`), the check belongs right after `resolve_chat_intent` returns a
`"trade"` result and before calling `store.record_incoming_signal` — add:

```python
    if intent["intent"] == "trade" and not settings.get("enabled"):
        store.add_chat_message(
            user_id, "assistant",
            "AI trading is currently disabled in your settings, so I can't act on that -- enable it on the AI Trading page first.",
        )
        on_event({"type": "done"})
        return
```

placed in `backend/ai_chat.py`'s `handle_chat_message`, immediately after the
`intent = resolve_chat_intent(message, settings)` line and before the
`if intent["intent"] == "question":` block. Since this modifies a Task 3 file, re-run
`tests/test_ai_chat.py` in Step 4 below too, and add one test there:

```python
# append to backend/tests/test_ai_chat.py
def test_handle_chat_message_trade_intent_rejected_when_ai_trading_disabled():
    disabled_settings = {**BASE_SETTINGS, "enabled": False}
    with patch("ai_chat.store.add_chat_message") as mock_add, \
         patch("ai_chat.ai_pipeline.bind_user_credentials"), \
         patch("ai_chat.ai_pipeline.data_agent_node", side_effect=lambda state, on_event=None: state), \
         patch("ai_chat.resolve_chat_intent", return_value={"intent": "trade", "symbol": "BTCUSD", "timeframe": "15m", "action": "buy"}), \
         patch("ai_chat.store.record_incoming_signal") as mock_record:
        events = []
        ai_chat.handle_chat_message("user_1", "go long BTC", disabled_settings, events.append)
    mock_record.assert_not_called()
    assert mock_add.call_count == 2  # user message + the "disabled" explanation
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:
```
cd /home/anand-shaurya/Documents/personal/delta/.worktrees/ai-trading-agents/backend
.venv/bin/python -m pytest tests/test_ai_chat.py tests/test_ai_webhook.py -v
```
Expected: all tests in both files pass.

- [ ] **Step 5: Run the full backend suite**

Run: `cd /home/anand-shaurya/Documents/personal/delta/.worktrees/ai-trading-agents/backend && .venv/bin/python -m pytest -v`
Expected: all pass, no regressions.

- [ ] **Step 6: Commit**

```bash
cd /home/anand-shaurya/Documents/personal/delta/.worktrees/ai-trading-agents
git add backend/main.py backend/ai_chat.py backend/tests/test_ai_chat.py backend/tests/test_ai_webhook.py
git commit -m "feat(main): add POST /ai/chat, GET /ai/chat/stream, GET /ai/chat/messages"
```

---

### Task 6: `frontend/lib/aiChat.ts` — types + pure helpers

**Files:**
- Create: `frontend/lib/aiChat.ts`
- Test: `frontend/lib/aiChat.test.ts`

**Interfaces:**
- Produces:
  - `type ChatMessage = { id: number; ts: number; role: "user" | "assistant"; content: string; signal_id: number | null }`.
  - `type ThinkingEvent = { node?: "data_agent" | "coordinator" | "executer"; type: "token" | "tool_call" | "guardrail_result" | "done" | "error"; [key: string]: any }`.
  - `parseSseLine(line: string) -> ThinkingEvent | null` — given one raw line from an
    SSE stream (e.g. `'data: {"type":"token","text":"hi"}'`), returns the parsed event
    object, or `null` for a blank line or a line not starting with `"data: "` (keep-alive
    or malformed — never throw).
  - `groupEventsByNode(events: ThinkingEvent[]) -> Record<string, ThinkingEvent[]>` —
    buckets events by `node` (events with no `node` field, e.g. `"done"`/`"error"`, go
    under the key `"_meta"`), preserving each bucket's original order.
  - `formatTokenStream(events: ThinkingEvent[]) -> string` — concatenates the `text`
    field of every event with `type === "token"`, in order, ignoring all other event
    types.

- [ ] **Step 1: Write the failing tests**

```ts
// frontend/lib/aiChat.test.ts
import { expect, it } from "vitest";
import { parseSseLine, groupEventsByNode, formatTokenStream, type ThinkingEvent } from "./aiChat";

it("parses a well-formed SSE data line", () => {
  const event = parseSseLine('data: {"type":"token","node":"coordinator","text":"hi"}');
  expect(event).toEqual({ type: "token", node: "coordinator", text: "hi" });
});

it("returns null for a blank or non-data line", () => {
  expect(parseSseLine("")).toBeNull();
  expect(parseSseLine(": keep-alive")).toBeNull();
});

it("returns null for malformed JSON rather than throwing", () => {
  expect(parseSseLine("data: not json")).toBeNull();
});

it("groups events by node, with no-node events under _meta", () => {
  const events: ThinkingEvent[] = [
    { node: "data_agent", type: "tool_call", name: "get_position" },
    { node: "coordinator", type: "token", text: "a" },
    { node: "coordinator", type: "token", text: "b" },
    { type: "done" },
  ];
  const grouped = groupEventsByNode(events);
  expect(grouped.data_agent).toHaveLength(1);
  expect(grouped.coordinator).toHaveLength(2);
  expect(grouped._meta).toEqual([{ type: "done" }]);
});

it("formats a token stream by concatenating text in order, ignoring other event types", () => {
  const events: ThinkingEvent[] = [
    { node: "coordinator", type: "token", text: "hel" },
    { node: "data_agent", type: "tool_call", name: "get_position" },
    { node: "coordinator", type: "token", text: "lo" },
  ];
  expect(formatTokenStream(events)).toBe("hello");
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /home/anand-shaurya/Documents/personal/delta/.worktrees/ai-trading-agents/frontend && npx vitest run lib/aiChat.test.ts`
Expected: FAIL — cannot find module `./aiChat`.

- [ ] **Step 3: Implement**

```ts
// frontend/lib/aiChat.ts
export type ChatMessage = {
  id: number;
  ts: number;
  role: "user" | "assistant";
  content: string;
  signal_id: number | null;
};

export type ThinkingEvent = {
  node?: "data_agent" | "coordinator" | "executer";
  type: "token" | "tool_call" | "guardrail_result" | "done" | "error";
  [key: string]: any;
};

export function parseSseLine(line: string): ThinkingEvent | null {
  if (!line.startsWith("data: ")) return null;
  const payload = line.slice("data: ".length).trim();
  if (!payload) return null;
  try {
    return JSON.parse(payload) as ThinkingEvent;
  } catch {
    return null;
  }
}

export function groupEventsByNode(events: ThinkingEvent[]): Record<string, ThinkingEvent[]> {
  const grouped: Record<string, ThinkingEvent[]> = {};
  for (const e of events) {
    const key = e.node ?? "_meta";
    (grouped[key] ??= []).push(e);
  }
  return grouped;
}

export function formatTokenStream(events: ThinkingEvent[]): string {
  return events
    .filter((e) => e.type === "token")
    .map((e) => e.text ?? "")
    .join("");
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /home/anand-shaurya/Documents/personal/delta/.worktrees/ai-trading-agents/frontend && npx vitest run lib/aiChat.test.ts`
Expected: 5 passed.

- [ ] **Step 5: Typecheck**

Run: `cd /home/anand-shaurya/Documents/personal/delta/.worktrees/ai-trading-agents/frontend && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
cd /home/anand-shaurya/Documents/personal/delta/.worktrees/ai-trading-agents
git add frontend/lib/aiChat.ts frontend/lib/aiChat.test.ts
git commit -m "feat(frontend): add AI chat types and SSE event parsing helpers"
```

---

### Task 7: `frontend/app/(app)/ai-trading/chat/page.tsx` — chat + live thinking UI

**Files:**
- Create: `frontend/app/(app)/ai-trading/chat/page.tsx`
- Modify: `frontend/app/(app)/ai-trading/page.tsx` — add a link to the new chat page.

**Interfaces:**
- Consumes: `api`/`ApiError` from `frontend/lib/api.ts`; `ChatMessage`/`ThinkingEvent`/
  `parseSseLine`/`groupEventsByNode`/`formatTokenStream` from `frontend/lib/aiChat.ts`
  (Task 6); `useToast` from `frontend/components/Toast`. Browser-native `EventSource`
  (no new npm dependency).

- [ ] **Step 1: Build the page**

```tsx
// frontend/app/(app)/ai-trading/chat/page.tsx
"use client";
import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { useToast } from "@/components/Toast";
import { type ChatMessage, type ThinkingEvent, parseSseLine, groupEventsByNode, formatTokenStream } from "@/lib/aiChat";

export default function AiChatPage() {
  const toast = useToast();
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [liveEvents, setLiveEvents] = useState<ThinkingEvent[]>([]);
  const esRef = useRef<EventSource | null>(null);

  async function loadMessages() {
    try {
      setMessages(await api<ChatMessage[]>("GET", "/ai/chat/messages?limit=100"));
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "Failed to load chat history", "err");
    }
  }

  useEffect(() => {
    loadMessages();
    return () => esRef.current?.close();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function send() {
    const message = input.trim();
    if (!message || sending) return;
    setInput("");
    setSending(true);
    setLiveEvents([]);
    try {
      await api("POST", "/ai/chat", { message });
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "Failed to send", "err");
      setSending(false);
      return;
    }
    const es = new EventSource("/api/ai/chat/stream");
    esRef.current = es;
    es.onmessage = (ev) => {
      const parsed = parseSseLine(`data: ${ev.data}`);
      if (!parsed) return;
      setLiveEvents((prev) => [...prev, parsed]);
      if (parsed.type === "done" || parsed.type === "error") {
        es.close();
        esRef.current = null;
        setSending(false);
        loadMessages();
      }
    };
    es.onerror = () => {
      es.close();
      esRef.current = null;
      setSending(false);
    };
  }

  const grouped = groupEventsByNode(liveEvents);
  const coordinatorText = formatTokenStream(grouped.coordinator ?? []);

  return (
    <div>
      <section>
        <h2>Chat with the coordinator</h2>
        <div className="autoRow" style={{ flexDirection: "column", gap: 8, maxWidth: 700 }}>
          {messages.map((m) => (
            <div
              key={m.id}
              style={{
                alignSelf: m.role === "user" ? "flex-end" : "flex-start",
                background: m.role === "user" ? "#1e3a5f" : "#2a2a2a",
                padding: "8px 12px",
                borderRadius: 8,
                maxWidth: "80%",
              }}
            >
              {m.signal_id != null && <span className="tag" style={{ marginRight: 6 }}>trade</span>}
              {m.content}
            </div>
          ))}
        </div>

        {sending && (
          <div className="stats" style={{ marginTop: 12 }}>
            <div className="stat">
              <small>data_agent</small>
              <b>{(grouped.data_agent ?? []).map((e) => (e.type === "tool_call" ? `called ${e.name}` : "")).join(", ") || "…"}</b>
            </div>
            <div className="stat">
              <small>coordinator</small>
              <b>{coordinatorText || "…"}</b>
            </div>
            <div className="stat">
              <small>executer</small>
              <b>
                {(grouped.executer ?? [])
                  .filter((e) => e.type === "guardrail_result")
                  .map((e) => (e.allowed ? "guardrails passed" : `rejected: ${e.reason}`))
                  .join(", ") || "…"}
              </b>
            </div>
          </div>
        )}

        <div className="autoRow" style={{ marginTop: 12 }}>
          <input
            style={{ flex: 1 }}
            value={input}
            disabled={sending}
            placeholder="Ask a question, or give a trade instruction..."
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && send()}
          />
          <button type="button" className="btn" disabled={sending} onClick={send}>
            {sending ? "Thinking…" : "Send"}
          </button>
        </div>
      </section>
    </div>
  );
}
```

- [ ] **Step 2: Add a link from the settings page**

In `frontend/app/(app)/ai-trading/page.tsx`, add near the top of the page (e.g. right
after the `<h2>AI Trading</h2>` heading):

```tsx
<a href="/ai-trading/chat" className="btn ghost sm" style={{ marginBottom: 10, display: "inline-block" }}>
  Chat with the coordinator →
</a>
```

- [ ] **Step 3: Typecheck and run the full frontend test suite**

Run: `cd /home/anand-shaurya/Documents/personal/delta/.worktrees/ai-trading-agents/frontend && npx tsc --noEmit && npx vitest run`
Expected: no type errors; all tests pass. This page has no dedicated test file, matching
the existing convention (same as `analysis/page.tsx` and `ai-trading/page.tsx` before
it) — only `lib/aiChat.ts`'s logic is unit-tested.

- [ ] **Step 4: Manual verification note**

A live browser check (typing a message, watching the stream) is valuable here but
requires a running backend with a real `OPENAI_API_KEY` and Delta credentials
configured — not something to automate in this task. Note in the task report whether a
manual check was performed and what was observed, but do not block DONE on it being
possible in this environment.

- [ ] **Step 5: Commit**

```bash
cd /home/anand-shaurya/Documents/personal/delta/.worktrees/ai-trading-agents
git add frontend/app/\(app\)/ai-trading/chat/page.tsx
git add -u
git commit -m "feat(frontend): add chat + live agent thinking page"
```

---

## After all tasks

Run the full test suite one more time end to end:
```
cd /home/anand-shaurya/Documents/personal/delta/.worktrees/ai-trading-agents/backend && .venv/bin/python -m pytest -v
cd /home/anand-shaurya/Documents/personal/delta/.worktrees/ai-trading-agents/frontend && npx vitest run && npx tsc --noEmit
```
before moving to a final whole-branch review and `finishing-a-development-branch`.
Given this plan modifies the already-shipped, safety-critical `ai_pipeline.py` nodes,
the final review should explicitly diff `coordinator_node`/`data_agent_node`/
`executer_node`'s behavior with `on_event=None` against their pre-this-plan behavior,
not just confirm tests pass.
