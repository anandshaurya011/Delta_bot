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
