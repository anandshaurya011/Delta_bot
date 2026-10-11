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
