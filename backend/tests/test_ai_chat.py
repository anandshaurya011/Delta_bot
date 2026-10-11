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
