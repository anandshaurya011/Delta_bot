"""LangGraph pipeline: coordinator -> data_agent -> executer.

Every node calls `call_llm`, a single thin wrapper, so tests mock one
function instead of the OpenAI client. Guardrail enforcement (Task 7) is
plain Python and never goes through `call_llm`.
"""

from __future__ import annotations

import json
from typing import Any, Callable, TypedDict

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
    signal_id: int | None
    settings: dict[str, Any]
    gathered_data: dict[str, Any]
    plan: dict[str, Any] | None
    order_request: dict[str, Any] | None
    execution_result: dict[str, Any] | None


def call_llm(
    model: str,
    system_prompt: str,
    user_content: str,
    tools: list[dict[str, Any]] | None = None,
    on_chunk: Callable[[str], None] | None = None,
    json_mode: bool = True,
) -> str:
    # The OpenAI API rejects response_format={"type": "json_object"} with a 400 unless
    # the word "json" appears somewhere in the messages -- every node in this pipeline
    # asks for structured JSON and satisfies that by construction, but free-text chat
    # replies (ai_chat.py's question-answering path) are plain prose, so json_mode=False
    # lets those calls opt out instead of tripping that 400 on every chat question.
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_content}],
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    if tools:
        kwargs["tools"] = tools
    if on_chunk is None:
        response = _get_client().chat.completions.create(**kwargs)
        return response.choices[0].message.content
    kwargs["stream"] = True
    full_text = ""
    for chunk in _get_client().chat.completions.create(**kwargs):
        delta = chunk.choices[0].delta.content
        if delta:
            on_chunk(delta)
            full_text += delta
    return full_text


_COORDINATOR_BASE_PROMPT = (
    "You are the coordinator agent for an automated trading system. You receive a "
    "TradingView signal and the data the data_agent gathered. Decide one action: "
    "open, add, reduce, flip, hold, or close. Respond ONLY with JSON: "
    '{"action": "...", "side": "long"|"short"|"none", "size_hint": number|null, "reasoning": "..."}'
)


def coordinator_node(
    state: PipelineState, on_event: Callable[[dict[str, Any]], None] | None = None
) -> PipelineState:
    settings = state["settings"]
    system_prompt = _COORDINATOR_BASE_PROMPT
    if settings.get("coordinator_prompt"):
        system_prompt += "\n\nUser instructions: " + settings["coordinator_prompt"]
    user_content = json.dumps({"signal": state["signal"], "gathered_data": state.get("gathered_data", {})})
    on_chunk = (lambda text: on_event({"node": "coordinator", "type": "token", "text": text})) if on_event else None
    raw = call_llm(
        model=settings["coordinator_model"], system_prompt=system_prompt, user_content=user_content, on_chunk=on_chunk
    )
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
    tasks run outside the request's contextvar scope.

    Always calls delta.use_credentials, even when no credentials are stored, exactly
    like auth.bind_user does for normal authed requests. delta_api._active() falls back
    to the operator's own global Delta API key ONLY when no per-call credentials were
    ever bound at all -- a conditional call here (skipping use_credentials when creds is
    None) would silently let this background pipeline trade on the operator's own Delta
    account on behalf of a user who never configured their own keys.
    """
    creds = store.get_credentials(user_id)
    delta.use_credentials(
        creds.api_key if creds else None,
        creds.api_secret if creds else None,
        creds.env if creds else "india_prod",
    )


_DATA_AGENT_BASE_PROMPT = (
    "You are the data_agent for an automated trading system. Given a signal and the "
    "available tools, decide which tools to call to gather the context the coordinator "
    f"needs. Available tools: {json.dumps(DATA_TOOLS)}. Respond ONLY with JSON: "
    '{"tool_calls": [{"name": "...", "args": {...}}, ...]}'
)


def data_agent_node(
    state: PipelineState, on_event: Callable[[dict[str, Any]], None] | None = None
) -> PipelineState:
    settings = state["settings"]
    system_prompt = _DATA_AGENT_BASE_PROMPT
    if settings.get("data_prompt"):
        system_prompt += "\n\nUser instructions: " + settings["data_prompt"]
    user_content = json.dumps({"signal": state["signal"]})
    on_chunk = (lambda text: on_event({"node": "data_agent", "type": "token", "text": text})) if on_event else None
    raw = call_llm(
        model=settings["data_model"], system_prompt=system_prompt, user_content=user_content, on_chunk=on_chunk
    )
    # The LLM is asked for {"tool_calls": [...]}, but nothing stops it from returning a
    # bare JSON array, a non-list "tool_calls" value, or list entries that aren't
    # {"name": ..., "args": {...}} objects -- any of which previously crashed the whole
    # pipeline with an uncaught AttributeError/KeyError/TypeError (seen in manual testing
    # via the chat UI). Gathering extra context is best-effort: a malformed response here
    # should degrade to "no extra data gathered", not take down the chat/webhook request.
    try:
        parsed = json.loads(raw)
        requested = parsed.get("tool_calls", []) if isinstance(parsed, dict) else []
        if not isinstance(requested, list):
            requested = []
    except (json.JSONDecodeError, TypeError):
        requested = []

    gathered: dict[str, Any] = {}
    for call in requested:
        if not isinstance(call, dict):
            continue
        name = call.get("name")
        impl = _TOOL_IMPL.get(name)
        if impl:
            args = call.get("args", {})
            if not isinstance(args, dict):
                args = {}
            if on_event:
                on_event({"node": "data_agent", "type": "tool_call", "name": name, "args": args})
            gathered[name] = impl(args)
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
            size = order.get("size", 0)
            unfilled = order.get("unfilled_size", size)
            if unfilled < size:
                return {"outcome": "partially_filled", "order": order}
            return {"outcome": "exchange_rejected", "order": order}
        if _time.time() >= deadline:
            return {"outcome": "unfilled_timeout", "order": order}
        _time.sleep(interval_s)


def executer_node(
    state: PipelineState, on_event: Callable[[dict[str, Any]], None] | None = None
) -> PipelineState:
    plan = state["plan"] or {}
    if plan.get("action") in (None, "hold") or plan.get("side") == "none":
        state["execution_result"] = {"executed": False, "reason": "no_action"}
        return state

    settings = state["settings"]
    system_prompt = _EXECUTER_BASE_PROMPT
    if settings.get("executer_prompt"):
        system_prompt += "\n\nUser instructions: " + settings["executer_prompt"]
    user_content = json.dumps({"plan": plan, "signal": state["signal"]})
    on_chunk = (lambda text: on_event({"node": "executer", "type": "token", "text": text})) if on_event else None
    raw = call_llm(
        model=settings["executer_model"], system_prompt=system_prompt, user_content=user_content, on_chunk=on_chunk
    )
    try:
        order_request = json.loads(raw)
        symbol = order_request["symbol"]
        side = order_request["side"]
        size = order_request["size"]
    except (json.JSONDecodeError, KeyError, TypeError):
        state["execution_result"] = {"executed": False, "reason": "invalid_order_request"}
        return state

    # Deterministic Python-only validation of the executer's LLM output, with zero
    # LLM involvement -- the LLM is never consulted on whether a trade is allowed.
    # This goes beyond "do the keys exist" (checked above) to validate VALUES, so
    # a negative/zero/float/bool size, a bogus side, or a symbol the executer's LLM
    # swapped in for the original signal's symbol can never reach guardrails/the exchange.
    if side not in ("buy", "sell"):
        state["execution_result"] = {"executed": False, "reason": "invalid_order_request"}
        return state
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        state["execution_result"] = {"executed": False, "reason": "invalid_order_request"}
        return state
    if symbol != state["signal"]["symbol"]:
        state["execution_result"] = {"executed": False, "reason": "invalid_order_request"}
        return state
    # The executer's LLM is supposed to translate the coordinator's size_hint into a
    # concrete order size, not invent an unrelated number -- reject anything wildly out
    # of proportion (seen in testing: size_hint 0.5 turned into size 50, a 100x jump).
    # A generous 10x band in either direction allows for real unit differences (e.g.
    # size_hint expressed as a risk fraction vs. size in whole contracts) while still
    # catching a genuinely disconnected value.
    size_hint = plan.get("size_hint")
    if isinstance(size_hint, (int, float)) and not isinstance(size_hint, bool) and size_hint > 0:
        ratio = size / size_hint
        if ratio > 10 or ratio < 0.1:
            state["execution_result"] = {"executed": False, "reason": "invalid_order_request"}
            return state

    state["order_request"] = order_request
    position = delta.get_position(symbol)
    existing_size = position["size"] if position else 0
    # KNOWN LIMITATION: the max_daily_loss guardrail is supposed to use today's
    # realized+unrealized P&L (per spec), but get_total_unrealized_pnl() returns
    # LIFETIME open unrealized P&L -- it does not reflect P&L realized earlier today
    # (e.g. from a position closed at a loss) and can be skewed by old open positions'
    # unrealized P&L that has nothing to do with today. Computing the correct figure
    # requires a new delta_api.py function that aggregates today's fills/wallet
    # transactions by date; that is not yet implemented, so this remains a known gap.
    context = {
        "existing_position_size": existing_size,
        "trades_today": store.get_trade_count_today(state["user_id"]),
        "daily_pnl": delta.get_total_unrealized_pnl(),
    }
    guardrail_result = evaluate_guardrails(order_request, settings.get("guardrails_json", {}), context)
    if on_event:
        on_event(
            {
                "node": "executer",
                "type": "guardrail_result",
                "allowed": guardrail_result.allowed,
                "reason": guardrail_result.reason,
            }
        )
    if not guardrail_result.allowed:
        state["execution_result"] = {"executed": False, "reason": guardrail_result.reason}
        return state

    if settings.get("paper_trading"):
        # Paper trading: every check above (validation, guardrails) ran exactly as it
        # would for a real trade -- only the exchange call is skipped, replaced with an
        # immediate simulated fill at the current market price. No real order is placed.
        price = delta.get_current_price(symbol)
        action = plan.get("action")
        if action in ("close", "flip"):
            # A real "close"/"flip" trade closes the existing exchange position via a
            # counter-order; a simulated position has no exchange side to close, so
            # explicitly close every open paper trade for this symbol here. "flip" then
            # falls through to open the new, opposite-direction paper trade below; "close"
            # does not -- there is nothing left to open.
            closed = store.close_all_open_paper_trades(state["user_id"], symbol, price)
            if action == "close":
                store.increment_trade_count(state["user_id"])
                state["execution_result"] = {
                    "executed": True,
                    "mode": "paper",
                    "action": "close",
                    "closed_trades": closed,
                    "fill_status": "filled",
                }
                return state
        else:
            closed = []

        trade_id = store.record_paper_trade(
            state["user_id"], symbol, order_request["side"], order_request["size"], price,
            signal_id=state.get("signal_id"),
        )
        store.increment_trade_count(state["user_id"])
        state["execution_result"] = {
            "executed": True,
            "mode": "paper",
            "paper_trade_id": trade_id,
            "fill_status": "filled",
            "order": {"symbol": symbol, "side": order_request["side"], "size": order_request["size"], "price": price},
            **({"closed_trades": closed} if closed else {}),
        }
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


def run_pipeline(
    user_id: str,
    signal: dict[str, Any],
    settings: dict[str, Any],
    on_event: Callable[[dict[str, Any]], None] | None = None,
    signal_id: int | None = None,
) -> dict[str, Any]:
    bind_user_credentials(user_id)
    state: PipelineState = {
        "user_id": user_id,
        "signal": signal,
        "signal_id": signal_id,
        "settings": settings,
        "gathered_data": {},
        "plan": None,
        "order_request": None,
        "execution_result": None,
    }
    state = data_agent_node(state, on_event=on_event)
    state = coordinator_node(state, on_event=on_event)
    state = executer_node(state, on_event=on_event)
    return {
        "gathered_data": state["gathered_data"],
        "plan": state["plan"],
        "order_request": state["order_request"],
        "execution_result": state["execution_result"],
    }
