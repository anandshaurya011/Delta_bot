import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

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


def test_bind_user_credentials_uses_stored_creds():
    fake_creds = MagicMock(api_key="k", api_secret="s", env="india_testnet")
    with patch("ai_pipeline.store.get_credentials", return_value=fake_creds) as mock_get, \
         patch("ai_pipeline.delta.use_credentials") as mock_use:
        ai_pipeline.bind_user_credentials("user_1")
    mock_get.assert_called_once_with("user_1")
    mock_use.assert_called_once_with("k", "s", "india_testnet")


def test_bind_user_credentials_fails_closed_when_user_has_no_stored_creds():
    # Regression: delta_api._active() falls back to the operator's own global Delta
    # API key only when use_credentials was never called at all in this context. If
    # bind_user_credentials no-ops for a user with no stored creds, this background
    # pipeline would silently trade on the operator's account instead of failing.
    with patch("ai_pipeline.store.get_credentials", return_value=None) as mock_get, \
         patch("ai_pipeline.delta.use_credentials") as mock_use:
        ai_pipeline.bind_user_credentials("user_1")
    mock_get.assert_called_once_with("user_1")
    mock_use.assert_called_once_with(None, None, "india_prod")


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


def test_wait_for_fill_missing_unfilled_size_key_falls_through_to_state_check():
    # Malformed/incomplete exchange response: no "unfilled_size" key at all.
    # Must NOT be treated as filled; should fall through to state-based logic.
    with patch("ai_pipeline.delta.get_order_by_id", return_value={"id": 1, "state": "cancelled"}):
        result = ai_pipeline.wait_for_fill(1, timeout_s=1, interval_s=0.01)
    assert result["outcome"] == "exchange_rejected"


def test_wait_for_fill_missing_unfilled_size_key_times_out_when_open():
    with patch("ai_pipeline.delta.get_order_by_id", return_value={"id": 1, "state": "open"}):
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


def test_executer_node_paper_trading_skips_real_order_and_records_paper_trade():
    state = _state(plan={"action": "open", "side": "long", "size_hint": 5, "reasoning": "go long"})
    state["settings"]["paper_trading"] = True
    order_response = json.dumps({"symbol": "BTCUSD", "side": "buy", "size": 5})
    with patch("ai_pipeline.call_llm", return_value=order_response), \
         patch("ai_pipeline.delta.get_position", return_value=None), \
         patch("ai_pipeline.delta.get_total_unrealized_pnl", return_value=0.0), \
         patch("ai_pipeline.delta.get_current_price", return_value=50000.0), \
         patch("ai_pipeline.store.get_trade_count_today", return_value=0), \
         patch("ai_pipeline.store.increment_trade_count") as mock_inc, \
         patch("ai_pipeline.store.record_paper_trade", return_value=7) as mock_record, \
         patch("ai_pipeline.delta.place_order") as mock_place, \
         patch("ai_pipeline.delta.get_order_by_id") as mock_get_order:
        state = ai_pipeline.executer_node(state)
    mock_place.assert_not_called()
    mock_get_order.assert_not_called()
    mock_record.assert_called_once_with("user_1", "BTCUSD", "buy", 5, 50000.0, signal_id=None)
    mock_inc.assert_called_once()
    assert state["execution_result"] == {
        "executed": True,
        "mode": "paper",
        "paper_trade_id": 7,
        "fill_status": "filled",
        "order": {"symbol": "BTCUSD", "side": "buy", "size": 5, "price": 50000.0},
    }


def test_executer_node_paper_trading_still_enforces_guardrails():
    state = _state(plan={"action": "open", "side": "long", "size_hint": 5, "reasoning": "go long"})
    state["settings"]["paper_trading"] = True
    state["settings"]["guardrails_json"] = {"max_position_size": {"enabled": True, "value": 1}}
    order_response = json.dumps({"symbol": "BTCUSD", "side": "buy", "size": 5})
    with patch("ai_pipeline.call_llm", return_value=order_response), \
         patch("ai_pipeline.delta.get_position", return_value=None), \
         patch("ai_pipeline.delta.get_total_unrealized_pnl", return_value=0.0), \
         patch("ai_pipeline.store.get_trade_count_today", return_value=0), \
         patch("ai_pipeline.store.record_paper_trade") as mock_record:
        state = ai_pipeline.executer_node(state)
    mock_record.assert_not_called()
    assert state["execution_result"]["executed"] is False


def test_executer_node_rejects_order_wildly_out_of_proportion_to_size_hint():
    # Regression: seen in manual testing, size_hint 0.5 turned into order size 50 (100x).
    state = _state(plan={"action": "open", "side": "long", "size_hint": 0.5, "reasoning": "go long"})
    order_response = json.dumps({"symbol": "BTCUSD", "side": "buy", "size": 50})
    with patch("ai_pipeline.call_llm", return_value=order_response), \
         patch("ai_pipeline.delta.place_order") as mock_place:
        state = ai_pipeline.executer_node(state)
    mock_place.assert_not_called()
    assert state["execution_result"] == {"executed": False, "reason": "invalid_order_request"}


def test_executer_node_allows_order_within_proportion_band_of_size_hint():
    state = _state(plan={"action": "open", "side": "long", "size_hint": 5, "reasoning": "go long"})
    order_response = json.dumps({"symbol": "BTCUSD", "side": "buy", "size": 8})
    placed_order = {"id": 43}
    filled_order = {"id": 43, "state": "closed", "unfilled_size": 0}
    with patch("ai_pipeline.call_llm", return_value=order_response), \
         patch("ai_pipeline.delta.get_position", return_value=None), \
         patch("ai_pipeline.delta.get_total_unrealized_pnl", return_value=0.0), \
         patch("ai_pipeline.store.get_trade_count_today", return_value=0), \
         patch("ai_pipeline.store.increment_trade_count"), \
         patch("ai_pipeline.delta.place_order", return_value=placed_order), \
         patch("ai_pipeline.delta.get_order_by_id", return_value=filled_order):
        state = ai_pipeline.executer_node(state)
    assert state["execution_result"]["executed"] is True


def test_executer_node_allows_order_when_size_hint_is_none():
    # size_hint can legitimately be null (e.g. coordinator leaves sizing to the executer) --
    # the proportion check must not reject every order in that case.
    state = _state(plan={"action": "open", "side": "long", "size_hint": None, "reasoning": "go long"})
    order_response = json.dumps({"symbol": "BTCUSD", "side": "buy", "size": 50})
    placed_order = {"id": 44}
    filled_order = {"id": 44, "state": "closed", "unfilled_size": 0}
    with patch("ai_pipeline.call_llm", return_value=order_response), \
         patch("ai_pipeline.delta.get_position", return_value=None), \
         patch("ai_pipeline.delta.get_total_unrealized_pnl", return_value=0.0), \
         patch("ai_pipeline.store.get_trade_count_today", return_value=0), \
         patch("ai_pipeline.store.increment_trade_count"), \
         patch("ai_pipeline.delta.place_order", return_value=placed_order), \
         patch("ai_pipeline.delta.get_order_by_id", return_value=filled_order):
        state = ai_pipeline.executer_node(state)
    assert state["execution_result"]["executed"] is True


def test_executer_node_handles_malformed_json_from_llm():
    state = _state(plan={"action": "open", "side": "long", "size_hint": 5, "reasoning": "go long"})
    with patch("ai_pipeline.call_llm", return_value="not valid json"), \
         patch("ai_pipeline.delta.place_order") as mock_place:
        state = ai_pipeline.executer_node(state)
    mock_place.assert_not_called()
    assert state["execution_result"] == {"executed": False, "reason": "invalid_order_request"}


def test_executer_node_handles_missing_required_field_from_llm():
    state = _state(plan={"action": "open", "side": "long", "size_hint": 5, "reasoning": "go long"})
    order_response = json.dumps({"symbol": "BTCUSD", "side": "buy"})  # missing "size"
    with patch("ai_pipeline.call_llm", return_value=order_response), \
         patch("ai_pipeline.delta.place_order") as mock_place:
        state = ai_pipeline.executer_node(state)
    mock_place.assert_not_called()
    assert state["execution_result"] == {"executed": False, "reason": "invalid_order_request"}


def test_executer_node_rejects_invalid_side():
    state = _state(plan={"action": "open", "side": "long", "size_hint": 5, "reasoning": "go long"})
    order_response = json.dumps({"symbol": "BTCUSD", "side": "hold", "size": 5})
    with patch("ai_pipeline.call_llm", return_value=order_response), \
         patch("ai_pipeline.delta.place_order") as mock_place:
        state = ai_pipeline.executer_node(state)
    mock_place.assert_not_called()
    assert state["execution_result"] == {"executed": False, "reason": "invalid_order_request"}


def test_executer_node_rejects_float_size():
    state = _state(plan={"action": "open", "side": "long", "size_hint": 5, "reasoning": "go long"})
    order_response = json.dumps({"symbol": "BTCUSD", "side": "buy", "size": 5.5})
    with patch("ai_pipeline.call_llm", return_value=order_response), \
         patch("ai_pipeline.delta.place_order") as mock_place:
        state = ai_pipeline.executer_node(state)
    mock_place.assert_not_called()
    assert state["execution_result"] == {"executed": False, "reason": "invalid_order_request"}


def test_executer_node_rejects_zero_or_negative_size():
    for bad_size in (0, -3):
        state = _state(plan={"action": "open", "side": "long", "size_hint": 5, "reasoning": "go long"})
        order_response = json.dumps({"symbol": "BTCUSD", "side": "buy", "size": bad_size})
        with patch("ai_pipeline.call_llm", return_value=order_response), \
             patch("ai_pipeline.delta.place_order") as mock_place:
            state = ai_pipeline.executer_node(state)
        mock_place.assert_not_called()
        assert state["execution_result"] == {"executed": False, "reason": "invalid_order_request"}


def test_executer_node_rejects_bool_size():
    state = _state(plan={"action": "open", "side": "long", "size_hint": 5, "reasoning": "go long"})
    order_response = json.dumps({"symbol": "BTCUSD", "side": "buy", "size": True})
    with patch("ai_pipeline.call_llm", return_value=order_response), \
         patch("ai_pipeline.delta.place_order") as mock_place:
        state = ai_pipeline.executer_node(state)
    mock_place.assert_not_called()
    assert state["execution_result"] == {"executed": False, "reason": "invalid_order_request"}


def test_executer_node_rejects_symbol_mismatch_with_signal():
    state = _state(plan={"action": "open", "side": "long", "size_hint": 5, "reasoning": "go long"})
    order_response = json.dumps({"symbol": "ETHUSD", "side": "buy", "size": 5})  # signal symbol is BTCUSD
    with patch("ai_pipeline.call_llm", return_value=order_response), \
         patch("ai_pipeline.delta.place_order") as mock_place:
        state = ai_pipeline.executer_node(state)
    mock_place.assert_not_called()
    assert state["execution_result"] == {"executed": False, "reason": "invalid_order_request"}


def test_executer_node_daily_pnl_context_comes_from_lifetime_unrealized_pnl():
    # Documents the known limitation (see comment in ai_pipeline.executer_node): daily_pnl
    # is currently sourced from get_total_unrealized_pnl() (lifetime open unrealized P&L),
    # not today's realized+unrealized P&L. This test pins that behavior so the code comment
    # can't silently drift from reality.
    state = _state(plan={"action": "open", "side": "long", "size_hint": 5, "reasoning": "go long"})
    order_response = json.dumps({"symbol": "BTCUSD", "side": "buy", "size": 5})
    placed_order = {"id": 42}
    filled_order = {"id": 42, "state": "closed", "unfilled_size": 0}
    with patch("ai_pipeline.call_llm", return_value=order_response), \
         patch("ai_pipeline.delta.get_position", return_value=None), \
         patch("ai_pipeline.delta.get_total_unrealized_pnl", return_value=-999.0) as mock_pnl, \
         patch("ai_pipeline.store.get_trade_count_today", return_value=0), \
         patch("ai_pipeline.store.increment_trade_count"), \
         patch("ai_pipeline.delta.place_order", return_value=placed_order), \
         patch("ai_pipeline.delta.get_order_by_id", return_value=filled_order), \
         patch("ai_pipeline.evaluate_guardrails", wraps=ai_pipeline.evaluate_guardrails) as mock_guard:
        ai_pipeline.executer_node(state)
    mock_pnl.assert_called_once()
    _, _, context = mock_guard.call_args[0]
    assert context["daily_pnl"] == -999.0


def test_wait_for_fill_cancelled_with_partial_fill_returns_partially_filled():
    with patch(
        "ai_pipeline.delta.get_order_by_id",
        return_value={"id": 1, "state": "cancelled", "size": 10, "unfilled_size": 4},
    ):
        result = ai_pipeline.wait_for_fill(1, timeout_s=1, interval_s=0.01)
    assert result["outcome"] == "partially_filled"


def test_wait_for_fill_cancelled_with_nothing_filled_returns_exchange_rejected():
    with patch(
        "ai_pipeline.delta.get_order_by_id",
        return_value={"id": 1, "state": "cancelled", "size": 10, "unfilled_size": 10},
    ):
        result = ai_pipeline.wait_for_fill(1, timeout_s=1, interval_s=0.01)
    assert result["outcome"] == "exchange_rejected"


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
    # NOTE: mocking ai_pipeline.call_llm directly (as other tests in this file do) would
    # bypass on_chunk entirely -- the Mock never invokes the callback -- so with an empty
    # tool_calls list and a "hold" plan (which short-circuits executer_node before any
    # guardrail event), zero events would ever be emitted regardless of whether on_event
    # threading is correct. To actually exercise on_event threading through run_pipeline,
    # this test mocks the client at the _get_client level and streams, like the
    # coordinator/data_agent token-event tests above.
    data_chunk = MagicMock()
    data_chunk.choices = [MagicMock(delta=MagicMock(content=json.dumps({"tool_calls": []})))]
    plan_chunk = MagicMock()
    plan_chunk.choices = [
        MagicMock(
            delta=MagicMock(
                content=json.dumps(
                    {"action": "hold", "side": "none", "size_hint": None, "reasoning": "no edge"}
                )
            )
        )
    ]
    with patch("ai_pipeline.bind_user_credentials"), \
         patch("ai_pipeline._get_client") as mock_get_client:
        mock_get_client.return_value.chat.completions.create.side_effect = [iter([data_chunk]), iter([plan_chunk])]
        events = []
        result = ai_pipeline.run_pipeline(
            "user_1", {"symbol": "BTCUSD", "timeframe": "15m", "action": "buy", "note": None},
            _state()["settings"], on_event=events.append,
        )
    assert result["plan"]["action"] == "hold"
    nodes_seen = {e["node"] for e in events}
    assert "data_agent" in nodes_seen
    assert "coordinator" in nodes_seen
