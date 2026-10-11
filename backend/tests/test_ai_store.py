import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("DELTA_DB_PATH", str(tmp_path / "test.db"))
    import store as store_module
    importlib.reload(store_module)
    return store_module


def test_get_or_create_ai_settings_creates_defaults(store):
    row = store.get_or_create_ai_settings("user_1")
    assert row["user_id"] == "user_1"
    assert row["enabled"] == 0
    assert row["cooldown_minutes"] == 30
    assert row["guardrails_json"] == {}
    assert row["webhook_token"]


def test_get_or_create_ai_settings_is_idempotent(store):
    first = store.get_or_create_ai_settings("user_1")
    second = store.get_or_create_ai_settings("user_1")
    assert first["webhook_token"] == second["webhook_token"]


def test_update_ai_settings(store):
    store.get_or_create_ai_settings("user_1")
    updated = store.update_ai_settings(
        "user_1", enabled=1, cooldown_minutes=15, guardrails_json={"max_trades_per_day": {"enabled": True, "value": 5}}
    )
    assert updated["enabled"] == 1
    assert updated["cooldown_minutes"] == 15
    assert updated["guardrails_json"]["max_trades_per_day"]["value"] == 5


def test_regenerate_webhook_token(store):
    original = store.get_or_create_ai_settings("user_1")
    new_token = store.regenerate_webhook_token("user_1")
    assert new_token != original["webhook_token"]
    assert store.get_user_id_by_webhook_token(original["webhook_token"]) is None
    # Note: webhook_token row is created with enabled=0 by default (see
    # test_get_or_create_ai_settings_creates_defaults), and regenerate_webhook_token
    # does not change `enabled`, so the new token still won't resolve until enabled.
    assert store.get_user_id_by_webhook_token(new_token) is None
    store.update_ai_settings("user_1", enabled=1)
    assert store.get_user_id_by_webhook_token(new_token) == "user_1"


def test_get_user_id_by_webhook_token_requires_enabled(store):
    row = store.get_or_create_ai_settings("user_1")
    assert store.get_user_id_by_webhook_token(row["webhook_token"]) is None
    store.update_ai_settings("user_1", enabled=1)
    assert store.get_user_id_by_webhook_token(row["webhook_token"]) == "user_1"


def test_record_incoming_signal_first_time_is_processing(store):
    result = store.record_incoming_signal("user_1", "BTCUSD", "15m", "buy", None, cooldown_minutes=30)
    assert result["status"] == "processing"
    assert result["id"] > 0


def test_record_incoming_signal_dedups_identical_payload(store):
    first = store.record_incoming_signal("user_1", "BTCUSD", "15m", "buy", "note", cooldown_minutes=30)
    store.update_signal_result(first["id"], "done", {"execution_result": {"executed": False}})
    second = store.record_incoming_signal("user_1", "BTCUSD", "15m", "buy", "note", cooldown_minutes=30)
    assert second["status"] == "duplicate_skipped"


def test_record_incoming_signal_single_flight_blocks_same_timeframe(store):
    first = store.record_incoming_signal("user_1", "BTCUSD", "15m", "buy", "a", cooldown_minutes=30)
    assert first["status"] == "processing"
    second = store.record_incoming_signal("user_1", "ETHUSD", "15m", "sell", "b", cooldown_minutes=30)
    assert second["status"] == "duplicate_skipped"


def test_record_incoming_signal_stale_processing_row_does_not_block(store, monkeypatch):
    import time as time_module

    real_time = time_module.time
    # Insert a "processing" row simulating a crash that happened well before the
    # staleness threshold (AI_PROCESSING_STALE_SECONDS).
    monkeypatch.setattr(time_module, "time", lambda: real_time() - store.AI_PROCESSING_STALE_SECONDS - 10)
    first = store.record_incoming_signal("user_1", "BTCUSD", "15m", "buy", "a", cooldown_minutes=30)
    assert first["status"] == "processing"

    monkeypatch.setattr(time_module, "time", real_time)
    second = store.record_incoming_signal("user_1", "ETHUSD", "15m", "sell", "b", cooldown_minutes=30)
    assert second["status"] == "processing"


def test_record_incoming_signal_different_timeframe_not_blocked(store):
    store.record_incoming_signal("user_1", "BTCUSD", "15m", "buy", "a", cooldown_minutes=30)
    other = store.record_incoming_signal("user_1", "BTCUSD", "1h", "buy", "a", cooldown_minutes=30)
    assert other["status"] == "processing"


def test_cooldown_only_starts_after_executed_trade(store):
    held = store.record_incoming_signal("user_1", "BTCUSD", "15m", "buy", "x1", cooldown_minutes=30)
    store.update_signal_result(held["id"], "done", {"execution_result": {"executed": False}})
    not_cooled = store.record_incoming_signal("user_1", "BTCUSD", "15m", "buy", "x2", cooldown_minutes=30)
    assert not_cooled["status"] == "processing"
    store.update_signal_result(not_cooled["id"], "done", {"execution_result": {"executed": True}})
    cooled = store.record_incoming_signal("user_1", "BTCUSD", "15m", "buy", "x3", cooldown_minutes=30)
    assert cooled["status"] == "cooldown_skipped"


def test_list_signals_returns_newest_first_with_parsed_result(store):
    a = store.record_incoming_signal("user_1", "BTCUSD", "15m", "buy", "a", cooldown_minutes=30)
    store.update_signal_result(a["id"], "done", {"plan": {"action": "hold"}})
    rows = store.list_signals("user_1", limit=10)
    assert rows[0]["id"] == a["id"]
    assert rows[0]["result_json"] == {"plan": {"action": "hold"}}


def test_trade_count_increments_and_reads_back(store):
    assert store.get_trade_count_today("user_1") == 0
    store.increment_trade_count("user_1")
    store.increment_trade_count("user_1")
    assert store.get_trade_count_today("user_1") == 2
