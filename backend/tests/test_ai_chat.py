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
    "enabled": True,
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


def test_handle_chat_message_passes_symbol_into_data_agent_signal():
    # Regression: chat questions like "get the data and tell me should I buy" never name
    # a symbol themselves -- without the frontend's currently-viewed symbol threaded in,
    # the data_agent has nothing to look up and (correctly but unhelpfully) says so.
    captured = {}

    def fake_data_agent(state, on_event=None):
        captured["signal"] = dict(state["signal"])
        return state

    with patch("ai_chat.store.add_chat_message"), \
         patch("ai_chat.ai_pipeline.bind_user_credentials"), \
         patch("ai_chat.ai_pipeline.data_agent_node", side_effect=fake_data_agent), \
         patch("ai_chat.resolve_chat_intent", return_value={"intent": "question"}), \
         patch("ai_chat.ai_pipeline.call_llm", return_value="BTC is around $65,000 right now."):
        ai_chat.handle_chat_message("user_1", "get the data and analyse", BASE_SETTINGS, lambda e: None, symbol="BTCUSD")
    assert captured["signal"]["symbol"] == "BTCUSD"
    assert captured["signal"]["note"] == "get the data and analyse"


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


def test_handle_chat_message_question_path_calls_llm_without_json_mode():
    # Regression: OpenAI rejects response_format=json_object with a 400 unless the word
    # "json" appears in the messages. The free-text chat answer prompt has no reason to
    # mention "json" (it's plain prose), so this call must opt out of json_mode entirely
    # rather than rely on the prompt text happening to satisfy the API's requirement.
    with patch("ai_chat.store.add_chat_message"), \
         patch("ai_chat.ai_pipeline.bind_user_credentials"), \
         patch("ai_chat.ai_pipeline.data_agent_node", side_effect=lambda state, on_event=None: state), \
         patch("ai_chat.resolve_chat_intent", return_value={"intent": "question"}), \
         patch("ai_chat.ai_pipeline.call_llm", return_value="BTC is around $65,000 right now.") as mock_call:
        ai_chat.handle_chat_message("user_1", "hi you there?", BASE_SETTINGS, lambda e: None)
    _, kwargs = mock_call.call_args
    assert kwargs["json_mode"] is False


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
    assert mock_add.call_count == 2
    assert mock_add.call_args_list[0].args[:2] == ("user_1", "user")
    assert mock_add.call_args_list[1].args[:2] == ("user_1", "assistant")


def test_handle_chat_message_trade_path_calls_record_incoming_signal_and_executer():
    with patch("ai_chat.store.add_chat_message"), \
         patch("ai_chat.ai_pipeline.bind_user_credentials"), \
         patch("ai_chat.ai_pipeline.data_agent_node", side_effect=lambda state, on_event=None: {**state, "gathered_data": {"price": 50000}}), \
         patch("ai_chat.resolve_chat_intent", return_value={"intent": "trade", "symbol": "BTCUSD", "timeframe": "15m", "action": "buy"}), \
         patch("ai_chat.store.record_incoming_signal", return_value={"id": 9, "status": "processing"}) as mock_record, \
         patch("ai_chat.ai_pipeline.coordinator_node", side_effect=lambda state, on_event=None: {**state, "plan": {"action": "hold", "side": "none", "size_hint": None, "reasoning": "x"}}), \
         patch("ai_chat.ai_pipeline.executer_node", side_effect=lambda state, on_event=None: {**state, "order_request": {"symbol": "BTCUSD", "side": "buy", "size": 0}, "execution_result": {"executed": False, "reason": "no_action"}}) as mock_exec, \
         patch("ai_chat.store.update_signal_result") as mock_update:
        events = []
        ai_chat.handle_chat_message("user_1", "go long BTC on the 15m", BASE_SETTINGS, events.append)
    mock_record.assert_called_once_with("user_1", "BTCUSD", "15m", "buy", "go long BTC on the 15m", 30)
    mock_exec.assert_called_once()
    mock_update.assert_called_once_with(9, "done", {
        "gathered_data": {"price": 50000},
        "plan": {"action": "hold", "side": "none", "size_hint": None, "reasoning": "x"},
        "order_request": {"symbol": "BTCUSD", "side": "buy", "size": 0},
        "execution_result": {"executed": False, "reason": "no_action"},
    })


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


def test_handle_chat_message_resolve_chat_intent_failure_still_emits_done_and_error():
    with patch("ai_chat.store.add_chat_message") as mock_add, \
         patch("ai_chat.ai_pipeline.bind_user_credentials"), \
         patch("ai_chat.ai_pipeline.data_agent_node", side_effect=lambda state, on_event=None: state), \
         patch("ai_chat.resolve_chat_intent", side_effect=RuntimeError("openai rate limit")):
        events = []
        ai_chat.handle_chat_message("user_1", "go long BTC", BASE_SETTINGS, events.append)
    done_events = [e for e in events if e.get("type") == "done"]
    error_events = [e for e in events if e.get("type") == "error"]
    assert len(done_events) == 1
    assert len(error_events) == 1
    assert "openai rate limit" in error_events[0]["message"]
    # user message + the error message
    assert mock_add.call_count == 2
    assert mock_add.call_args_list[1].args[:2] == ("user_1", "assistant")


def test_handle_chat_message_data_agent_node_failure_still_emits_done_and_error():
    with patch("ai_chat.store.add_chat_message") as mock_add, \
         patch("ai_chat.ai_pipeline.bind_user_credentials"), \
         patch("ai_chat.ai_pipeline.data_agent_node", side_effect=RuntimeError("data agent exploded")):
        events = []
        ai_chat.handle_chat_message("user_1", "what's the price of BTC?", BASE_SETTINGS, events.append)
    done_events = [e for e in events if e.get("type") == "done"]
    error_events = [e for e in events if e.get("type") == "error"]
    assert len(done_events) == 1
    assert len(error_events) == 1
    assert "data agent exploded" in error_events[0]["message"]
    assert mock_add.call_count == 2


def test_handle_chat_message_question_path_call_llm_failure_still_emits_done_and_error():
    with patch("ai_chat.store.add_chat_message") as mock_add, \
         patch("ai_chat.ai_pipeline.bind_user_credentials"), \
         patch("ai_chat.ai_pipeline.data_agent_node", side_effect=lambda state, on_event=None: state), \
         patch("ai_chat.resolve_chat_intent", return_value={"intent": "question"}), \
         patch("ai_chat.ai_pipeline.call_llm", side_effect=RuntimeError("openai timeout")):
        events = []
        ai_chat.handle_chat_message("user_1", "what's the price of BTC?", BASE_SETTINGS, events.append)
    done_events = [e for e in events if e.get("type") == "done"]
    error_events = [e for e in events if e.get("type") == "error"]
    assert len(done_events) == 1
    assert len(error_events) == 1
    assert "openai timeout" in error_events[0]["message"]
    assert mock_add.call_count == 2
