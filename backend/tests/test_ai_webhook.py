import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DELTA_DB_PATH", str(tmp_path / "test.db"))
    import importlib
    import store as store_module
    importlib.reload(store_module)
    import main as main_module
    importlib.reload(main_module)
    async def _fake_bind_user():
        main_module.auth.user_id_var.set("user_1")
        return "user_1"

    main_module.app.dependency_overrides[main_module.auth.bind_user] = _fake_bind_user
    try:
        yield TestClient(main_module.app)
    finally:
        main_module.app.dependency_overrides.clear()


def test_get_ai_settings_creates_default_row(client):
    resp = client.get("/ai/settings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False or body["enabled"] == 0
    assert body["webhook_token"]


def test_put_ai_settings_updates_fields(client):
    client.get("/ai/settings")
    resp = client.put("/ai/settings", json={"enabled": True, "cooldown_minutes": 10})
    assert resp.status_code == 200
    assert resp.json()["cooldown_minutes"] == 10


def test_regenerate_webhook_token_invalidates_old_one(client):
    original = client.get("/ai/settings").json()["webhook_token"]
    resp = client.post("/ai/webhook/regenerate")
    assert resp.status_code == 200
    new_token = resp.json()["webhook_token"]
    assert new_token != original
    assert client.post(f"/webhook/tv/{original}", json={"symbol": "BTCUSD", "timeframe": "15m", "action": "buy"}).status_code == 404


def test_webhook_unknown_token_is_404(client):
    resp = client.post("/webhook/tv/not-a-real-token", json={"symbol": "BTCUSD", "timeframe": "15m", "action": "buy"})
    assert resp.status_code == 404


def test_webhook_disabled_is_404(client):
    token = client.get("/ai/settings").json()["webhook_token"]
    resp = client.post(f"/webhook/tv/{token}", json={"symbol": "BTCUSD", "timeframe": "15m", "action": "buy"})
    assert resp.status_code == 404


def test_webhook_enabled_queues_pipeline_and_returns_200(client):
    client.put("/ai/settings", json={"enabled": True})
    token = client.get("/ai/settings").json()["webhook_token"]
    with patch("main.ai_pipeline.run_pipeline") as mock_run:
        resp = client.post(f"/webhook/tv/{token}", json={"symbol": "BTCUSD", "timeframe": "15m", "action": "buy", "note": "n"})
    assert resp.status_code == 200
    signals = client.get("/ai/signals").json()
    assert signals[0]["status"] in ("processing", "done")


def test_webhook_duplicate_payload_is_skipped(client):
    client.put("/ai/settings", json={"enabled": True})
    token = client.get("/ai/settings").json()["webhook_token"]
    payload = {"symbol": "BTCUSD", "timeframe": "15m", "action": "buy", "note": "n"}
    with patch("main.ai_pipeline.run_pipeline"):
        client.post(f"/webhook/tv/{token}", json=payload)
        client.post(f"/webhook/tv/{token}", json=payload)
    signals = client.get("/ai/signals").json()
    assert signals[0]["status"] == "duplicate_skipped"


def test_webhook_works_with_no_auth_override_and_no_authorization_header(client):
    # Regression test: the webhook is mounted on a sub-app with no Clerk dependency,
    # so it must work even when nothing has overridden auth.bind_user. TradingView
    # can never send a Clerk session token, so this has to pass with zero auth headers.
    client.put("/ai/settings", json={"enabled": True})
    token = client.get("/ai/settings").json()["webhook_token"]
    import main as main_module

    main_module.app.dependency_overrides.clear()
    try:
        with patch("main.ai_pipeline.run_pipeline"):
            resp = client.post(
                f"/webhook/tv/{token}",
                json={"symbol": "BTCUSD", "timeframe": "15m", "action": "buy"},
                headers={},
            )
    finally:
        async def _fake_bind_user():
            main_module.auth.user_id_var.set("user_1")
            return "user_1"

        main_module.app.dependency_overrides[main_module.auth.bind_user] = _fake_bind_user
    assert resp.status_code == 200
    assert resp.json()["status"] == "processing"


def test_webhook_missing_fields_is_422(client):
    client.put("/ai/settings", json={"enabled": True})
    token = client.get("/ai/settings").json()["webhook_token"]
    resp = client.post(f"/webhook/tv/{token}", json={"symbol": "BTCUSD"})
    assert resp.status_code == 422
