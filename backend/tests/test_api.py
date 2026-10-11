import threading
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

import delta_api as delta


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DELTA_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("CREDENTIAL_ENC_KEY", Fernet.generate_key().decode())
    import auth, main
    monkeypatch.setattr(auth, "verify_token", lambda t: t)       # token == user id
    return TestClient(main.app, raise_server_exceptions=False)


def H(uid): return {"Authorization": f"Bearer {uid}"}


@pytest.fixture
def fake_delta(monkeypatch):
    calls = []
    def fake_request(method, path, query=None, body=None, auth=True):
        key, secret, base = delta._active()
        if auth and not (key and secret):
            raise delta.CredentialsMissing("no creds")
        calls.append((key, base, path))
        return []
    monkeypatch.setattr(delta, "_request", fake_request)
    return calls


def test_requires_token(client):
    assert client.get("/balances").status_code == 401
    assert client.get("/balances", headers={"Authorization": "Basic x"}).status_code == 401


def test_no_keys_gives_412_even_if_operator_globals_set(client, fake_delta, monkeypatch):
    monkeypatch.setattr(delta, "API_KEY", "OPERATOR"); monkeypatch.setattr(delta, "API_SECRET", "OPS")
    r = client.get("/balances", headers=H("u1"))
    assert r.status_code == 412 and r.json()["detail"] == "delta_credentials_missing"


def test_settings_lifecycle_masks_secret(client, fake_delta):
    assert client.get("/settings/delta", headers=H("u1")).json() == {"is_set": False, "env": None, "api_key_masked": None}
    r = client.put("/settings/delta", headers=H("u1"), json={"api_key": "ABCDEFGH1234", "api_secret": "topsecret", "env": "india_testnet"})
    assert r.status_code == 200 and r.json() == {"is_set": True, "env": "india_testnet", "api_key_masked": "••••1234"}
    assert "topsecret" not in r.text and "topsecret" not in client.get("/settings/delta", headers=H("u1")).text
    assert client.get("/balances", headers=H("u1")).status_code == 200
    assert client.delete("/settings/delta", headers=H("u1")).json() == {"is_set": False}
    assert client.get("/balances", headers=H("u1")).status_code == 412


def test_invalid_env_rejected_and_nothing_stored(client, fake_delta):
    r = client.put("/settings/delta", headers=H("u1"), json={"api_key": "k", "api_secret": "s", "env": "mars"})
    assert r.status_code == 422
    assert client.get("/settings/delta", headers=H("u1")).json()["is_set"] is False


def test_rejected_by_delta_is_not_stored(client, monkeypatch):
    def boom(*a, **k): raise delta.DeltaAPIError(401, {"error": {"code": "invalid_api_key"}})
    monkeypatch.setattr(delta, "_request", boom)
    r = client.put("/settings/delta", headers=H("u1"), json={"api_key": "k", "api_secret": "s", "env": "india_prod"})
    assert r.status_code == 400
    assert client.get("/settings/delta", headers=H("u1")).json()["is_set"] is False


def test_users_use_their_own_keys(client, fake_delta):
    for uid, key in (("u1", "KEY-ONE-1111"), ("u2", "KEY-TWO-2222")):
        assert client.put("/settings/delta", headers=H(uid), json={"api_key": key, "api_secret": "s", "env": "india_prod"}).status_code == 200
    fake_delta.clear()
    client.get("/balances", headers=H("u1")); client.get("/balances", headers=H("u2"))
    assert [c[0] for c in fake_delta] == ["KEY-ONE-1111", "KEY-TWO-2222"]


def test_concurrent_requests_do_not_leak_keys(client, fake_delta):
    for uid, key in (("u1", "AAAA1111"), ("u2", "BBBB2222")):
        client.put("/settings/delta", headers=H(uid), json={"api_key": key, "api_secret": "s", "env": "india_prod"})
    fake_delta.clear(); bad = []
    def hit(uid, key):
        for _ in range(20):
            r = client.get("/balances", headers=H(uid))
            if r.status_code != 200: bad.append(r.status_code)
    ts = [threading.Thread(target=hit, args=a) for a in (("u1", "AAAA1111"), ("u2", "BBBB2222"))]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert not bad and {c[0] for c in fake_delta} == {"AAAA1111", "BBBB2222"}
    assert len([c for c in fake_delta if c[0] == "AAAA1111"]) == 20


def test_audit_log_is_per_user(client, fake_delta, monkeypatch):
    monkeypatch.setattr(delta, "set_leverage", lambda s, l: {"ok": True})
    for uid in ("u1", "u2"):
        client.put("/settings/delta", headers=H(uid), json={"api_key": "k" * 8, "api_secret": "s", "env": "india_prod"})
    assert client.put("/leverage/BTCUSD", headers=H("u1"), json={"leverage": 5}).status_code == 200
    assert [e["action"] for e in client.get("/history", headers=H("u1")).json()] == ["set-leverage"]
    assert client.get("/history", headers=H("u2")).json() == []
