"""LangGraph pipeline: coordinator -> data_agent -> executer.

Every node calls `call_llm`, a single thin wrapper, so tests mock one
function instead of the OpenAI client. Guardrail enforcement (Task 7) is
plain Python and never goes through `call_llm`.
"""

from __future__ import annotations

import json
from typing import Any, TypedDict

from openai import OpenAI

import delta_api as delta
import store

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI()
    return _client


class PipelineState(TypedDict, total=False):
    user_id: str
    signal: dict[str, Any]
    settings: dict[str, Any]
    gathered_data: dict[str, Any]
    plan: dict[str, Any] | None
    order_request: dict[str, Any] | None
    execution_result: dict[str, Any] | None


def call_llm(model: str, system_prompt: str, user_content: str, tools: list[dict[str, Any]] | None = None) -> str:
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_content}],
        "response_format": {"type": "json_object"},
    }
    if tools:
        kwargs["tools"] = tools
    response = _get_client().chat.completions.create(**kwargs)
    return response.choices[0].message.content


_COORDINATOR_BASE_PROMPT = (
    "You are the coordinator agent for an automated trading system. You receive a "
    "TradingView signal and the data the data_agent gathered. Decide one action: "
    "open, add, reduce, flip, hold, or close. Respond ONLY with JSON: "
    '{"action": "...", "side": "long"|"short"|"none", "size_hint": number|null, "reasoning": "..."}'
)


def coordinator_node(state: PipelineState) -> PipelineState:
    settings = state["settings"]
    system_prompt = _COORDINATOR_BASE_PROMPT
    if settings.get("coordinator_prompt"):
        system_prompt += "\n\nUser instructions: " + settings["coordinator_prompt"]
    user_content = json.dumps({"signal": state["signal"], "gathered_data": state.get("gathered_data", {})})
    raw = call_llm(model=settings["coordinator_model"], system_prompt=system_prompt, user_content=user_content)
    state["plan"] = json.loads(raw)
    return state


DATA_TOOLS = [
    {"name": "get_position", "args": {"symbol": "str"}},
    {"name": "get_price", "args": {"symbol": "str"}},
    {"name": "get_analysis", "args": {"symbol": "str", "resolution": "str"}},
    {"name": "get_balances", "args": {}},
]

_TOOL_IMPL = {
    "get_position": lambda args: delta.get_position(args["symbol"]),
    "get_price": lambda args: delta.get_current_price(args["symbol"]),
    "get_analysis": lambda args: delta.analyze_symbol(args["symbol"], resolutions=[args.get("resolution", "1h")]),
    "get_balances": lambda args: delta.get_balances(),
}


def bind_user_credentials(user_id: str) -> None:
    """Looks up stored Delta credentials for user_id and binds them into delta_api's
    per-call context. Must be called once at the start of run_pipeline since background
    tasks run outside the request's contextvar scope."""
    creds = store.get_credentials(user_id)
    if creds:
        delta.use_credentials(creds.api_key, creds.api_secret, creds.env)


_DATA_AGENT_BASE_PROMPT = (
    "You are the data_agent for an automated trading system. Given a signal and the "
    "available tools, decide which tools to call to gather the context the coordinator "
    f"needs. Available tools: {json.dumps(DATA_TOOLS)}. Respond ONLY with JSON: "
    '{"tool_calls": [{"name": "...", "args": {...}}, ...]}'
)


def data_agent_node(state: PipelineState) -> PipelineState:
    settings = state["settings"]
    system_prompt = _DATA_AGENT_BASE_PROMPT
    if settings.get("data_prompt"):
        system_prompt += "\n\nUser instructions: " + settings["data_prompt"]
    user_content = json.dumps({"signal": state["signal"]})
    raw = call_llm(model=settings["data_model"], system_prompt=system_prompt, user_content=user_content)
    requested = json.loads(raw).get("tool_calls", [])
    gathered: dict[str, Any] = {}
    for call in requested:
        impl = _TOOL_IMPL.get(call["name"])
        if impl:
            gathered[call["name"]] = impl(call.get("args", {}))
    state["gathered_data"] = gathered
    return state


import time as _time

from ai_guardrails import evaluate_guardrails

_EXECUTER_BASE_PROMPT = (
    "You are the executer agent. Given the coordinator's plan, produce the concrete order "
    "request. You do not decide whether the trade is allowed — that is checked separately. "
    'Respond ONLY with JSON: {"symbol": "...", "side": "buy"|"sell", "size": integer}'
)


def wait_for_fill(order_id: int, timeout_s: float = 10.0, interval_s: float = 1.0) -> dict[str, Any]:
    deadline = _time.time() + timeout_s
    while True:
        order = delta.get_order_by_id(order_id)
        state = order.get("state")
        if "unfilled_size" in order and order["unfilled_size"] == 0:
            return {"outcome": "filled", "order": order}
        if state == "closed":
            return {"outcome": "partially_filled", "order": order}
        if state in ("cancelled", "rejected"):
            return {"outcome": "exchange_rejected", "order": order}
        if _time.time() >= deadline:
            return {"outcome": "unfilled_timeout", "order": order}
        _time.sleep(interval_s)


def executer_node(state: PipelineState) -> PipelineState:
    plan = state["plan"] or {}
    if plan.get("action") in (None, "hold") or plan.get("side") == "none":
        state["execution_result"] = {"executed": False, "reason": "no_action"}
        return state

    settings = state["settings"]
    system_prompt = _EXECUTER_BASE_PROMPT
    if settings.get("executer_prompt"):
        system_prompt += "\n\nUser instructions: " + settings["executer_prompt"]
    user_content = json.dumps({"plan": plan, "signal": state["signal"]})
    raw = call_llm(model=settings["executer_model"], system_prompt=system_prompt, user_content=user_content)
    try:
        order_request = json.loads(raw)
        symbol = order_request["symbol"]
        order_request["side"]
        order_request["size"]
    except (json.JSONDecodeError, KeyError, TypeError):
        state["execution_result"] = {"executed": False, "reason": "invalid_order_request"}
        return state
    state["order_request"] = order_request
    position = delta.get_position(symbol)
    existing_size = position["size"] if position else 0
    context = {
        "existing_position_size": existing_size,
        "trades_today": store.get_trade_count_today(state["user_id"]),
        "daily_pnl": delta.get_total_unrealized_pnl(),
    }
    guardrail_result = evaluate_guardrails(order_request, settings.get("guardrails_json", {}), context)
    if not guardrail_result.allowed:
        state["execution_result"] = {"executed": False, "reason": guardrail_result.reason}
        return state

    order = delta.place_order(symbol, order_request["side"], order_request["size"])
    fill = wait_for_fill(order["id"])
    if fill["outcome"] in ("filled", "partially_filled"):
        store.increment_trade_count(state["user_id"])
        state["execution_result"] = {"executed": True, "order": fill["order"], "fill_status": fill["outcome"]}
    else:
        state["execution_result"] = {"executed": False, "reason": fill["outcome"], "order": fill["order"]}
    return state


from langgraph.graph import StateGraph, END

_graph = StateGraph(PipelineState)
_graph.add_node("data_agent", data_agent_node)
_graph.add_node("coordinator", coordinator_node)
_graph.add_node("executer", executer_node)
_graph.set_entry_point("data_agent")
_graph.add_edge("data_agent", "coordinator")
_graph.add_edge("coordinator", "executer")
_graph.add_edge("executer", END)
_compiled_graph = _graph.compile()


def run_pipeline(user_id: str, signal: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    bind_user_credentials(user_id)
    initial_state: PipelineState = {
        "user_id": user_id,
        "signal": signal,
        "settings": settings,
        "gathered_data": {},
        "plan": None,
        "order_request": None,
        "execution_result": None,
    }
    final_state = _compiled_graph.invoke(initial_state)
    return {
        "gathered_data": final_state["gathered_data"],
        "plan": final_state["plan"],
        "order_request": final_state["order_request"],
        "execution_result": final_state["execution_result"],
    }
