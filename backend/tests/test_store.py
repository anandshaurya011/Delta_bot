import sqlite3
import pytest
from cryptography.fernet import Fernet


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("DELTA_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("CREDENTIAL_ENC_KEY", Fernet.generate_key().decode())


def test_credentials_roundtrip_and_encrypted_on_disk(tmp_path):
    import store
    store.save_credentials("u1", "KEY123", "SECRET456", "india_prod")
    c = store.get_credentials("u1")
    assert (c.api_key, c.api_secret, c.env) == ("KEY123", "SECRET456", "india_prod")
    raw = sqlite3.connect(tmp_path / "t.db").execute("select * from credentials").fetchall()
    assert "SECRET456" not in str(raw) and "KEY123" not in str(raw)


def test_credentials_isolated_replaced_deleted():
    import store
    store.save_credentials("u1", "a", "b", "india_prod")
    store.save_credentials("u2", "c", "d", "india_testnet")
    store.save_credentials("u1", "a2", "b2", "india_prod")
    assert store.get_credentials("u1").api_key == "a2"
    assert store.get_credentials("u2").api_key == "c"
    assert store.delete_credentials("u1") is True
    assert store.get_credentials("u1") is None
    assert store.delete_credentials("u1") is False


def test_events_scoped_filtered_newest_first_and_capped(monkeypatch):
    import store
    monkeypatch.setattr(store, "MAX_EVENTS_PER_USER", 3)
    for i in range(5):
        store.log_event("u1", "buy" if i % 2 else "sell", {"i": i}, True, {"r": i})
    store.log_event("u2", "buy", {}, False, "x")
    ev = store.list_events("u1")
    assert [e["request"]["i"] for e in ev] == [4, 3, 2]          # capped, newest first
    assert all(e["ok"] is True for e in ev)
    assert [e["action"] for e in store.list_events("u1", action="buy")] == ["buy"]   # of i=2,3,4 only i=3 is a buy
    assert len(store.list_events("u2")) == 1
    assert store.list_events("nobody") == []
