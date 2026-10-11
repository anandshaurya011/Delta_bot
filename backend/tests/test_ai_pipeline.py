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
