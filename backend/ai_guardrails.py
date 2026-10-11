"""Deterministic, Python-only guardrail checks for the AI executer.

No LLM is ever consulted for this decision — the coordinator/executer agents
produce a proposed order; this module is the sole authority on whether it is
allowed to execute.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GuardrailResult:
    allowed: bool
    reason: str | None = None
    disable_webhook: bool = False


def evaluate_guardrails(order_request: dict[str, Any], guardrails: dict[str, Any], context: dict[str, Any]) -> GuardrailResult:
    allowlist = guardrails.get("symbol_allowlist", {})
    if allowlist.get("enabled") and order_request["symbol"] not in allowlist.get("value", []):
        return GuardrailResult(False, "guardrail:symbol_allowlist")

    max_size = guardrails.get("max_position_size", {})
    if max_size.get("enabled"):
        direction = 1 if order_request["side"] == "buy" else -1
        resulting = context.get("existing_position_size", 0) + direction * order_request["size"]
        if abs(resulting) > max_size.get("value", 0):
            return GuardrailResult(False, "guardrail:max_position_size")

    max_trades = guardrails.get("max_trades_per_day", {})
    if max_trades.get("enabled") and context.get("trades_today", 0) >= max_trades.get("value", 0):
        return GuardrailResult(False, "guardrail:max_trades_per_day")

    max_loss = guardrails.get("max_daily_loss", {})
    if max_loss.get("enabled") and context.get("daily_pnl", 0.0) <= -abs(max_loss.get("value", 0)):
        return GuardrailResult(False, "guardrail:max_daily_loss", disable_webhook=True)

    return GuardrailResult(True)
