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
    except (json.JSONDecodeError, TypeError):
        return {"intent": "question"}
    if not isinstance(parsed, dict) or parsed.get("intent") != "trade":
        return {"intent": "question"}
    if not all(parsed.get(k) for k in _REQUIRED_TRADE_KEYS):
        return {"intent": "question"}
    return {"intent": "trade", **{k: parsed[k] for k in _REQUIRED_TRADE_KEYS}}


def handle_chat_message(user_id: str, message: str, settings: dict[str, Any], on_event: Callable[[dict[str, Any]], None]) -> None:
    try:
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

        if intent["intent"] == "trade" and not settings.get("enabled"):
            store.add_chat_message(
                user_id, "assistant",
                "AI trading is currently disabled in your settings, so I can't act on that -- enable it on the AI Trading page first.",
            )
            return

        if intent["intent"] == "question":
            on_chunk = lambda text: on_event({"node": "coordinator", "type": "token", "text": text})
            reply = ai_pipeline.call_llm(
                model=settings["coordinator_model"],
                system_prompt=_CHAT_ANSWER_PROMPT,
                user_content=json.dumps({"message": message, "gathered_data": state["gathered_data"]}),
                on_chunk=on_chunk,
            )
            store.add_chat_message(user_id, "assistant", reply)
            return

        symbol, timeframe, action = intent["symbol"], intent["timeframe"], intent["action"]
        outcome = store.record_incoming_signal(user_id, symbol, timeframe, action, message, settings["cooldown_minutes"])
        if outcome["status"] != "processing":
            store.add_chat_message(
                user_id, "assistant",
                f"Skipped ({outcome['status']}) -- a recent signal for {symbol} {timeframe} is already in flight or cooling down.",
                signal_id=outcome["id"],
            )
            return

        state["signal"] = {"symbol": symbol, "timeframe": timeframe, "action": action, "note": message}
        state["signal_id"] = outcome["id"]
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
            if (result["execution_result"] or {}).get("reason") == "guardrail:max_daily_loss":
                store.update_ai_settings(user_id, enabled=False)
            plan = result["plan"] or {}
            execution = result["execution_result"] or {}
            summary = str(plan.get("reasoning", "")) + (
                f" -> executed: {execution.get('executed')}" if "executed" in execution else ""
            )
            store.add_chat_message(user_id, "assistant", summary or "Done.", signal_id=outcome["id"])
        except Exception as exc:  # pragma: no cover - defensive, mirrors main.py's _run_signal_pipeline
            store.update_signal_result(outcome["id"], "error", {"error": str(exc)})
            store.add_chat_message(user_id, "assistant", f"Something went wrong: {exc}", signal_id=outcome["id"])
            on_event({"type": "error", "message": str(exc)})
    except Exception as exc:
        try:
            store.add_chat_message(user_id, "assistant", f"Something went wrong: {exc}")
        except Exception:
            pass
        try:
            on_event({"type": "error", "message": str(exc)})
        except Exception:
            pass
    finally:
        try:
            on_event({"type": "done"})
        except Exception:
            pass
