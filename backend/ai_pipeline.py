"""LangGraph pipeline: coordinator -> data_agent -> executer.

Every node calls `call_llm`, a single thin wrapper, so tests mock one
function instead of the OpenAI client. Guardrail enforcement (Task 7) is
plain Python and never goes through `call_llm`.
"""

from __future__ import annotations

import json
from typing import Any, TypedDict

from openai import OpenAI

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
