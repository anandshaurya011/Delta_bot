# Task 7 Report: executer node, guardrails, fill verification

## Summary
Implemented `wait_for_fill` and `executer_node` in `backend/ai_pipeline.py`, exactly per the brief, following TDD.

## What was done
1. Appended the 6 required test cases to `backend/tests/test_ai_pipeline.py` (test_wait_for_fill_* x3, test_executer_node_* x3).
2. Ran `pytest tests/test_ai_pipeline.py -v` and confirmed the new 6 tests failed with `AttributeError` (functions not yet defined) while the pre-existing 5 passed.
3. Appended `wait_for_fill(order_id, timeout_s=10.0, interval_s=1.0)` and `executer_node(state)` to `backend/ai_pipeline.py`, plus `_EXECUTER_BASE_PROMPT` and the `ai_guardrails.evaluate_guardrails` import, exactly as specified in the brief.
4. Re-ran tests: all 11 tests in `test_ai_pipeline.py` passed.
5. Ran the full backend suite: 58 passed, 0 failed, no regressions.
6. Committed: `backend/ai_pipeline.py` and `backend/tests/test_ai_pipeline.py`.

## Safety properties verified
- Guardrail enforcement (`evaluate_guardrails`) is plain deterministic Python — `call_llm` is only used once in `executer_node` to translate the plan into `{symbol, side, size}` JSON, and is never asked whether a trade is allowed. Guardrail rejection short-circuits before `delta.place_order` is ever called (verified by `mock_place.assert_not_called()` in the guardrail-rejection test).
- `hold`/no-action plans short-circuit via `reason: "no_action"` before any guardrail check or LLM call (verified by `mock_place.assert_not_called()` and no `call_llm` patch needed in that test).
- Order acceptance is distinguished from fill: after `delta.place_order`, `wait_for_fill` polls `delta.get_order_by_id` and only `filled`/`partially_filled` outcomes lead to `store.increment_trade_count` and `executed: True`. `unfilled_timeout` and `exchange_rejected` outcomes produce `executed: False` and do NOT increment the trade count.

## Test results
- `backend/tests/test_ai_pipeline.py`: 11 passed (5 pre-existing + 6 new).
- Full backend suite (`pytest`): 58 passed, 0 failed.

## Commit
`fbab6c8` — "feat: add executer node with python-enforced guardrails and fill verification" (branch `feat/ai-trading-agents`, worktree).

## Concerns
None. Implementation matches the brief verbatim; no deviations were needed.

## Fix round (review follow-up, 2026-10-11)

Reviewer raised two Important findings; both fixed.

### Finding 1 — `wait_for_fill` missing `unfilled_size` key silently treated as filled
Changed `unfilled = order.get("unfilled_size", 0); if unfilled == 0: ...` to
`if "unfilled_size" in order and order["unfilled_size"] == 0: ...`, so a malformed/incomplete
exchange response (key absent entirely) now falls through to the existing state-based checks
(`closed`/`cancelled`/`rejected`/timeout) instead of being treated as "filled".

### Finding 2 — `executer_node` uncaught JSON/KeyError on malformed LLM order_request
Wrapped `json.loads(raw)` and the `order_request["symbol"]`/`["side"]`/`["size"]` lookups in a
`try/except (json.JSONDecodeError, KeyError, TypeError)` that sets
`state["execution_result"] = {"executed": False, "reason": "invalid_order_request"}` and returns,
instead of propagating the exception (which would have left the signal's `ai_signals` row stuck
at `status="processing"` forever).

### New tests added (backend/tests/test_ai_pipeline.py)
- `test_wait_for_fill_missing_unfilled_size_key_falls_through_to_state_check`
- `test_wait_for_fill_missing_unfilled_size_key_times_out_when_open`
- `test_executer_node_handles_malformed_json_from_llm`
- `test_executer_node_handles_missing_required_field_from_llm`

### Test command and output

```
$ backend/.venv/bin/python -m pytest tests/test_ai_pipeline.py -v
============================= test session starts ==============================
platform linux -- Python 3.14.4, pytest-9.1.1, pluggy-1.6.0 -- .../backend/.venv/bin/python
collected 15 items

tests/test_ai_pipeline.py::test_coordinator_node_parses_plan_from_llm_response PASSED [  6%]
tests/test_ai_pipeline.py::test_coordinator_node_includes_custom_instructions_in_prompt PASSED [ 13%]
tests/test_ai_pipeline.py::test_bind_user_credentials_uses_stored_creds PASSED [ 20%]
tests/test_ai_pipeline.py::test_data_agent_node_runs_requested_tool_calls_and_fills_gathered_data PASSED [ 26%]
tests/test_ai_pipeline.py::test_data_agent_node_with_no_tool_calls_leaves_gathered_data_empty PASSED [ 33%]
tests/test_ai_pipeline.py::test_wait_for_fill_returns_filled_when_unfilled_size_zero PASSED [ 40%]
tests/test_ai_pipeline.py::test_wait_for_fill_returns_exchange_rejected PASSED [ 46%]
tests/test_ai_pipeline.py::test_wait_for_fill_times_out_when_still_open PASSED [ 53%]
tests/test_ai_pipeline.py::test_wait_for_fill_missing_unfilled_size_key_falls_through_to_state_check PASSED [ 60%]
tests/test_ai_pipeline.py::test_wait_for_fill_missing_unfilled_size_key_times_out_when_open PASSED [ 66%]
tests/test_ai_pipeline.py::test_executer_node_holds_without_calling_llm_or_placing_order PASSED [ 73%]
tests/test_ai_pipeline.py::test_executer_node_rejects_via_guardrail_without_placing_order PASSED [ 80%]
tests/test_ai_pipeline.py::test_executer_node_places_order_and_confirms_fill PASSED [ 86%]
tests/test_ai_pipeline.py::test_executer_node_handles_malformed_json_from_llm PASSED [ 93%]
tests/test_ai_pipeline.py::test_executer_node_handles_missing_required_field_from_llm PASSED [100%]

============================== 15 passed in 0.32s ==============================
```

Full backend suite:

```
$ backend/.venv/bin/python -m pytest
============================= test session starts ==============================
collected 62 items

tests/test_ai_guardrails.py .......                                      [ 11%]
tests/test_ai_pipeline.py ...............                                [ 35%]
tests/test_ai_store.py ............                                      [ 54%]
tests/test_api.py ........                                               [ 67%]
tests/test_auth.py .......                                               [ 79%]
tests/test_crypto.py ...                                                 [ 83%]
tests/test_delta_context.py .......                                      [ 95%]
tests/test_store.py ...                                                  [100%]

============================== 62 passed in 0.81s ==============================
```

### Concerns
None. Both fixes are minimal, targeted changes with direct test coverage; no regressions.
