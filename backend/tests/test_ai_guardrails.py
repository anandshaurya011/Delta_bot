# backend/tests/test_ai_guardrails.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_guardrails import evaluate_guardrails

BASE_CONTEXT = {"existing_position_size": 0, "trades_today": 0, "daily_pnl": 0.0}


def test_all_guardrails_disabled_always_allows():
    order = {"symbol": "DOGEUSD", "side": "buy", "size": 10_000}
    guardrails = {
        "max_position_size": {"enabled": False, "value": 1},
        "symbol_allowlist": {"enabled": False, "value": []},
        "max_trades_per_day": {"enabled": False, "value": 0},
        "max_daily_loss": {"enabled": False, "value": 0},
    }
    result = evaluate_guardrails(order, guardrails, BASE_CONTEXT)
    assert result.allowed is True


def test_symbol_allowlist_rejects_unlisted_symbol():
    order = {"symbol": "DOGEUSD", "side": "buy", "size": 1}
    guardrails = {"symbol_allowlist": {"enabled": True, "value": ["BTCUSD"]}}
    result = evaluate_guardrails(order, guardrails, BASE_CONTEXT)
    assert result.allowed is False
    assert result.reason == "guardrail:symbol_allowlist"


def test_max_position_size_rejects_when_resulting_size_too_large():
    order = {"symbol": "BTCUSD", "side": "buy", "size": 40}
    guardrails = {"max_position_size": {"enabled": True, "value": 50}}
    context = {**BASE_CONTEXT, "existing_position_size": 20}
    result = evaluate_guardrails(order, guardrails, context)
    assert result.allowed is False
    assert result.reason == "guardrail:max_position_size"


def test_max_position_size_allows_within_cap():
    order = {"symbol": "BTCUSD", "side": "buy", "size": 10}
    guardrails = {"max_position_size": {"enabled": True, "value": 50}}
    context = {**BASE_CONTEXT, "existing_position_size": 20}
    result = evaluate_guardrails(order, guardrails, context)
    assert result.allowed is True


def test_max_trades_per_day_rejects_at_cap():
    order = {"symbol": "BTCUSD", "side": "buy", "size": 1}
    guardrails = {"max_trades_per_day": {"enabled": True, "value": 3}}
    context = {**BASE_CONTEXT, "trades_today": 3}
    result = evaluate_guardrails(order, guardrails, context)
    assert result.allowed is False
    assert result.reason == "guardrail:max_trades_per_day"


def test_max_daily_loss_rejects_and_requests_webhook_disable():
    order = {"symbol": "BTCUSD", "side": "sell", "size": 1}
    guardrails = {"max_daily_loss": {"enabled": True, "value": 200}}
    context = {**BASE_CONTEXT, "daily_pnl": -250.0}
    result = evaluate_guardrails(order, guardrails, context)
    assert result.allowed is False
    assert result.reason == "guardrail:max_daily_loss"
    assert result.disable_webhook is True


def test_guardrail_missing_from_config_is_treated_as_disabled():
    order = {"symbol": "BTCUSD", "side": "buy", "size": 9999}
    result = evaluate_guardrails(order, {}, BASE_CONTEXT)
    assert result.allowed is True
