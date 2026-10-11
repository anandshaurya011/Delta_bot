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


def test_post_ai_chat_question_does_not_require_ai_trading_enabled(client):
    with patch("main.ai_chat.handle_chat_message") as mock_handle:
        resp = client.post("/ai/chat", json={"message": "what's the price of BTC?"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "processing"
    mock_handle.assert_called_once()
    args = mock_handle.call_args.args
    assert args[0] == "user_1"
    assert args[1] == "what's the price of BTC?"


def test_post_ai_chat_empty_message_is_422(client):
    resp = client.post("/ai/chat", json={"message": ""})
    assert resp.status_code == 422


def test_get_ai_chat_messages_returns_store_contents(client):
    import store
    store.add_chat_message("user_1", "user", "hello")
    resp = client.get("/ai/chat/messages")
    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["content"] == "hello"


def test_get_ai_chat_stream_yields_published_events(client):
    import ai_chat_stream
    ai_chat_stream.publish("user_1", {"type": "token", "text": "hi"})
    ai_chat_stream.publish("user_1", {"type": "done"})
    resp = client.get("/ai/chat/stream")
    assert resp.status_code == 200
    assert "data:" in resp.text
    assert '"type": "done"' in resp.text or '"type":"done"' in resp.text


def test_publish_failure_inside_on_event_does_not_mark_trade_as_error(client):
    """Mandatory hardening (Task 4 review carryover): ai_chat_stream.publish() can raise
    queue.Full under rare contention. It runs as on_event from INSIDE
    handle_chat_message's trade-path try/except, so a raw lambda calling publish would
    get caught there and misreport a successful trade as an "error" outcome. The
    on_event callback built in main.py must swallow that failure instead."""
    import main as main_module
    import store as store_module

    captured = {}
    orig_update_signal_result = store_module.update_signal_result

    def spy_update_signal_result(signal_id, status, result):
        captured["status"] = status
        return orig_update_signal_result(signal_id, status, result)

    with patch("main.ai_chat_stream.publish", side_effect=RuntimeError("queue.Full boom")), \
         patch.object(store_module, "update_signal_result", side_effect=spy_update_signal_result), \
         patch("main.ai_pipeline.bind_user_credentials"), \
         patch("main.ai_pipeline.data_agent_node", side_effect=lambda state, on_event=None: state), \
         patch("main.ai_chat.resolve_chat_intent", return_value={"intent": "trade", "symbol": "BTCUSD", "timeframe": "15m", "action": "buy"}), \
         patch("main.ai_pipeline.coordinator_node", side_effect=lambda state, on_event=None: {**state, "plan": {"reasoning": "ok"}}), \
         patch("main.ai_pipeline.executer_node", side_effect=lambda state, on_event=None: {**state, "execution_result": {"executed": True}}):
        settings = store_module.get_or_create_ai_settings("user_1")
        settings["enabled"] = True
        main_module._run_chat_message("user_1", "go long BTC", settings)

    assert captured.get("status") != "error"
