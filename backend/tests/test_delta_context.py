import threading
import pytest
import delta_api as delta


class _Resp:
    def __init__(self, body=b'{"success": true, "result": []}'): self.body = body
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return self.body


def _capture(monkeypatch, seen):
    def fake_urlopen(req, timeout=0):
        seen.append((req.full_url, req.get_header("Api-key")))
        return _Resp()
    monkeypatch.setattr(delta.urllib.request, "urlopen", fake_urlopen)


def test_context_creds_and_env_used(monkeypatch):
    seen = []; _capture(monkeypatch, seen)
    tok = delta.use_credentials("K1", "S1", "india_testnet")
    try:
        delta._request("GET", "/v2/wallet/balances")
    finally:
        delta._CREDS.reset(tok)
    assert seen == [(delta.BASE_URLS["india_testnet"] + "/v2/wallet/balances", "K1")]


def test_empty_context_never_falls_back_to_globals(monkeypatch):
    monkeypatch.setattr(delta, "API_KEY", "OPERATOR"); monkeypatch.setattr(delta, "API_SECRET", "OPSECRET")
    seen = []; _capture(monkeypatch, seen)
    tok = delta.use_credentials(None, None, "india_prod")
    try:
        with pytest.raises(delta.CredentialsMissing):
            delta._request("GET", "/v2/wallet/balances")
    finally:
        delta._CREDS.reset(tok)
    assert seen == []


def test_no_context_uses_globals_for_cli(monkeypatch):
    monkeypatch.setattr(delta, "API_KEY", "OPERATOR"); monkeypatch.setattr(delta, "API_SECRET", "OPSECRET")
    seen = []; _capture(monkeypatch, seen)
    delta._request("GET", "/v2/wallet/balances")
    assert seen[0][1] == "OPERATOR"


def test_threads_are_isolated(monkeypatch):
    seen = []; _capture(monkeypatch, seen)
    barrier = threading.Barrier(2)

    def worker(key):
        delta.use_credentials(key, "s", "india_prod")
        barrier.wait()
        delta._request("GET", "/x")

    ts = [threading.Thread(target=worker, args=(k,)) for k in ("A", "B")]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert sorted(k for _, k in seen) == ["A", "B"]


def test_signature_uses_context_secret(monkeypatch):
    sigs = []
    monkeypatch.setattr(delta.urllib.request, "urlopen", lambda req, timeout=0: (sigs.append(req.get_header("Signature")), _Resp())[1])
    monkeypatch.setattr(delta.time, "time", lambda: 1000)
    for secret in ("one", "two"):
        tok = delta.use_credentials("k", secret, "india_prod")
        delta._request("GET", "/p")
        delta._CREDS.reset(tok)
    assert sigs[0] != sigs[1]


def test_product_cache_is_per_environment(monkeypatch):
    calls = []
    monkeypatch.setattr(delta, "get_product", lambda s: (calls.append(delta.active_env()), {"id": len(calls)})[1])
    delta._PRODUCT_CACHE.clear()
    t1 = delta.use_credentials("k", "s", "india_prod"); a = delta.get_product_id("BTCUSD"); delta._CREDS.reset(t1)
    t2 = delta.use_credentials("k", "s", "india_testnet"); b = delta.get_product_id("BTCUSD"); delta._CREDS.reset(t2)
    assert a != b and calls == ["india_prod", "india_testnet"]
