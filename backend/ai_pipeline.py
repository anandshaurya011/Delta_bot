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
