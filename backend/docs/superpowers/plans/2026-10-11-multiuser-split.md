# Multi-user Split (Next.js + Clerk + per-user Delta keys) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the app into a FastAPI API and a Next.js/TypeScript frontend, with Clerk login and per-user Delta Exchange credentials.

**Architecture:** Browser -> Next.js (Clerk middleware, `/api/[...path]` proxy attaching the Clerk session token) -> FastAPI on localhost (verifies Clerk JWT via JWKS, binds the user's decrypted Delta keys into a `ContextVar` that `delta_api.py` reads) -> Delta. SQLite stores Fernet-encrypted credentials and a per-user audit log.

**Tech Stack:** Python 3.14 venv, FastAPI, PyJWT[crypto], cryptography, sqlite3, pytest + httpx; Next.js 15 (App Router), React 19, TypeScript, @clerk/nextjs 6, @tanstack/react-query 5.

**Spec:** `docs/superpowers/specs/2026-10-11-multiuser-split-design.md`

> Project root is `/home/anand-shaurya/Documents/personal/delta`. `delta_api.py` lives in that root; the API in `backend/`; the new app in `frontend/`. This is not a git repository, so there are no commit steps; each task ends with a verification step instead. If the user runs `git init`, commit at the end of each task.

## Global Constraints

- Secrets (`CLERK_SECRET_KEY`, `CREDENTIAL_ENC_KEY`, Delta key/secret) never appear in logs, API responses, committed files or test output. They live only in git-ignored `.env` / `.env.local` (mode 600).
- `GET /settings/delta` never returns the secret; the key is masked to its last 4 chars.
- A user with no saved Delta keys must NEVER fall back to the operator's `DELTA_API_KEY` env/`.env` globals inside the API process.
- Delta environments: only `india_prod` and `india_testnet` (the keys of `delta.BASE_URLS`).
- Missing keys -> HTTP `412` with `{"detail": "delta_credentials_missing"}`. Bad/missing token -> `401`.
- FastAPI binds `127.0.0.1`; no CORS; no static `/ui` mount after the migration.
- Clerk issuer/JWKS derived from the publishable key: `base64url(pk.split("_",2)[2])` minus trailing `$` -> `https://<domain>/.well-known/jwks.json`.
- Frontend env names: `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`, `CLERK_SECRET_KEY`, `BACKEND_URL` (default `http://127.0.0.1:8000`).
- Audit log capped at 5000 events per user.

## Review Focus

- Two users hit the API concurrently with different keys: each Delta request must be signed with its own key (Task 3, Task 5 tests).
- A signed-in user with no saved keys while the operator's `.env` holds `DELTA_API_KEY`: must get 412, not the operator's account (Task 3 test).
- Expired, wrong-issuer, wrong-algorithm (`alg=none`/HS256) or missing-`sub` tokens: all 401 (Task 4 tests).
- Saving keys Delta rejects, or saving with an invalid env: nothing is stored, clear 400/422 (Task 5 tests).
- Re-saving keys for the same user replaces them; deleting then calling Delta gives 412 (Task 2, Task 5 tests).
- Session expires mid-use in the browser (proxy gets 401): UI redirects to sign-in rather than showing a raw error (Task 7).

## File Structure

```
delta/delta_api.py                   MODIFY  per-request credential context, product cache keyed by base URL
delta/backend/
  requirements.txt                   CREATE
  config.py                          CREATE  get_or_create_env_secret()
  crypto.py                          CREATE  Fernet encrypt/decrypt
  store.py                           CREATE  SQLite: credentials + events (replaces db.py)
  auth.py                            CREATE  Clerk JWT verification + bind_user dependency
  main.py                            MODIFY  new auth, 412 handler, settings endpoints, scoped audit/history
  db.py                              DELETE  (after Task 5)
  tests/{conftest,test_crypto,test_store,test_delta_context,test_auth,test_api}.py  CREATE
delta/frontend/                      CREATE  Next.js app (see Tasks 6-12)
```

---

### Task 1: Backend dependencies, config and crypto

**Files:**
- Create: `backend/requirements.txt`, `backend/config.py`, `backend/crypto.py`, `backend/tests/conftest.py`, `backend/tests/test_crypto.py`

**Interfaces:**
- Produces: `config.get_or_create_secret(name: str, factory: Callable[[], str], env_path: Path = ENV_PATH) -> str`; `crypto.encrypt(plain: str) -> str`; `crypto.decrypt(token: str) -> str`.

- [ ] **Step 1: Install dependencies**

```bash
cd /home/anand-shaurya/Documents/personal/delta/backend
cat > requirements.txt <<'EOF'
fastapi
uvicorn
python-dotenv
pyjwt[crypto]
cryptography
httpx
pytest
EOF
.venv/bin/python -m ensurepip 2>/dev/null; .venv/bin/python -m pip install -r requirements.txt
```
Expected: installs succeed (`pip` may need `ensurepip`; if the venv has no pip, use `uv pip install -r requirements.txt --python .venv/bin/python`).

- [ ] **Step 2: Write failing tests**

`backend/tests/conftest.py`:
```python
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))            # delta_api.py
sys.path.insert(0, str(ROOT / "backend"))
```
`backend/tests/test_crypto.py`:
```python
import config, crypto


def test_get_or_create_secret_persists(tmp_path):
    env = tmp_path / ".env"
    a = config.get_or_create_secret("X_KEY", lambda: "v1", env)
    b = config.get_or_create_secret("X_KEY", lambda: "v2", env)
    assert a == b == "v1"
    assert oct(env.stat().st_mode & 0o777) == "0o600"


def test_env_var_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("X_KEY", "fromenv")
    assert config.get_or_create_secret("X_KEY", lambda: "v", tmp_path / ".env") == "fromenv"


def test_roundtrip_and_ciphertext_differs(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("CREDENTIAL_ENC_KEY", Fernet.generate_key().decode())
    token = crypto.encrypt("s3cret")
    assert token != "s3cret" and "s3cret" not in token
    assert crypto.decrypt(token) == "s3cret"
```

- [ ] **Step 3: Run to verify failure**

Run: `cd backend && .venv/bin/python -m pytest tests/test_crypto.py -v`
Expected: FAIL (`ModuleNotFoundError: config`).

- [ ] **Step 4: Implement**

`backend/config.py`:
```python
"""Env-backed secrets: read from the process env, then backend/.env, else generate and persist (chmod 600)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

ENV_PATH = Path(__file__).resolve().parent / ".env"


def get_or_create_secret(name: str, factory: Callable[[], str], env_path: Path = ENV_PATH) -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    if env_path.is_file():
        for line in env_path.read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() == name and v.strip():
                return v.strip().strip('"').strip("'")
    value = factory()
    fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(f"{name}={value}\n")
    return value
```
`backend/crypto.py`:
```python
"""Fernet encryption for Delta credentials at rest. Key: CREDENTIAL_ENC_KEY (generated on first use)."""

from __future__ import annotations

from cryptography.fernet import Fernet

import config


def _fernet() -> Fernet:
    return Fernet(config.get_or_create_secret("CREDENTIAL_ENC_KEY", lambda: Fernet.generate_key().decode()).encode())


def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()
```

- [ ] **Step 5: Verify passing**

Run: `cd backend && .venv/bin/python -m pytest tests/test_crypto.py -v`
Expected: 3 passed.

---

### Task 2: SQLite store (credentials + per-user audit events)

**Files:**
- Create: `backend/store.py`, `backend/tests/test_store.py`

**Interfaces:**
- Consumes: `crypto.encrypt/decrypt`.
- Produces:
  - `store.Creds` dataclass: `api_key: str`, `api_secret: str`, `env: str`.
  - `store.save_credentials(user_id: str, api_key: str, api_secret: str, env: str) -> None`
  - `store.get_credentials(user_id: str) -> Creds | None`
  - `store.delete_credentials(user_id: str) -> bool`
  - `store.log_event(user_id: str, action: str, request: Any, ok: bool, result: Any) -> None`
  - `store.list_events(user_id: str, limit: int = 100, action: str | None = None) -> list[dict]`
  - DB path from env `DELTA_DB_PATH`, default `backend/data.db`.

- [ ] **Step 1: Write failing tests**

`backend/tests/test_store.py`:
```python
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
```

- [ ] **Step 2: Run, expect FAIL** (`ModuleNotFoundError: store`):
`cd backend && .venv/bin/python -m pytest tests/test_store.py -v`

- [ ] **Step 3: Implement `backend/store.py`**

```python
"""SQLite store: encrypted per-user Delta credentials and a per-user audit log."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import crypto

MAX_EVENTS_PER_USER = 5000
_DEFAULT_PATH = Path(__file__).resolve().parent / "data.db"


@dataclass(frozen=True)
class Creds:
    api_key: str
    api_secret: str
    env: str


def _conn() -> sqlite3.Connection:
    path = Path(os.environ.get("DELTA_DB_PATH") or _DEFAULT_PATH)
    new = not path.exists()
    if new:
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))
    c = sqlite3.connect(path, timeout=10)
    c.execute("create table if not exists credentials(user_id text primary key, api_key_enc text not null, api_secret_enc text not null, env text not null, updated_at integer not null)")
    c.execute("create table if not exists events(id integer primary key autoincrement, user_id text not null, ts integer not null, action text not null, request text, ok integer not null, result text)")
    c.execute("create index if not exists ev_user on events(user_id, id)")
    return c


def save_credentials(user_id: str, api_key: str, api_secret: str, env: str) -> None:
    with closing(_conn()) as c, c:
        c.execute("insert or replace into credentials values(?,?,?,?,?)",
                  (user_id, crypto.encrypt(api_key), crypto.encrypt(api_secret), env, int(time.time())))


def get_credentials(user_id: str) -> Creds | None:
    with closing(_conn()) as c:
        row = c.execute("select api_key_enc, api_secret_enc, env from credentials where user_id=?", (user_id,)).fetchone()
    return Creds(crypto.decrypt(row[0]), crypto.decrypt(row[1]), row[2]) if row else None


def delete_credentials(user_id: str) -> bool:
    with closing(_conn()) as c, c:
        return c.execute("delete from credentials where user_id=?", (user_id,)).rowcount > 0


def log_event(user_id: str, action: str, request: Any, ok: bool, result: Any) -> None:
    with closing(_conn()) as c, c:
        c.execute("insert into events(user_id, ts, action, request, ok, result) values(?,?,?,?,?,?)",
                  (user_id, int(time.time()), action, json.dumps(request, default=str), int(ok), json.dumps(result, default=str)))
        c.execute("delete from events where user_id=? and id not in (select id from events where user_id=? order by id desc limit ?)",
                  (user_id, user_id, MAX_EVENTS_PER_USER))


def list_events(user_id: str, limit: int = 100, action: str | None = None) -> list[dict[str, Any]]:
    q, args = "select ts, action, request, ok, result from events where user_id=?", [user_id]
    if action:
        q += " and action=?"; args.append(action)
    q += " order by id desc limit ?"; args.append(limit)
    with closing(_conn()) as c:
        rows = c.execute(q, args).fetchall()
    return [{"ts": ts, "action": a, "request": json.loads(rq), "ok": bool(ok), "result": json.loads(rs)} for ts, a, rq, ok, rs in rows]
```
(The events response shape matches the old `db.list_events` so the UI is unchanged.)

- [ ] **Step 4: Verify passing**

Run: `cd backend && .venv/bin/python -m pytest tests/test_store.py -v`
Expected: 3 passed.

---

### Task 3: Per-request credentials in `delta_api.py`

**Files:**
- Modify: `delta_api.py:121-152` (`_sign`, `_request`), `delta_api.py:188-192` (`get_product_id`), add helpers after line 105.
- Create: `backend/tests/test_delta_context.py`

**Interfaces:**
- Produces:
  - `delta.CredentialsMissing(RuntimeError)`
  - `delta.use_credentials(api_key: str | None, api_secret: str | None, env: str) -> contextvars.Token` — sets the per-request context; empty/None keys mean "context active, no credentials" (so the process-global keys are NOT used).
  - `delta.active_env() -> str`
- Behavior: with no context set (CLI), globals are used exactly as before.

- [ ] **Step 1: Write failing tests**

`backend/tests/test_delta_context.py`:
```python
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
```

- [ ] **Step 2: Run, expect FAIL** (`AttributeError: use_credentials`):
`cd backend && .venv/bin/python -m pytest tests/test_delta_context.py -v`

- [ ] **Step 3: Implement in `delta_api.py`**

Add `import contextvars` with the other imports. After line 105 (`_PRODUCT_CACHE = ...`) change the cache type and add the context:
```python
_PRODUCT_CACHE: dict[tuple[str, str], dict[str, Any]] = {}

# Per-request credentials (set by the web backend). None = not in a web request -> module globals (CLI).
_CREDS: contextvars.ContextVar[tuple[str | None, str | None, str] | None] = contextvars.ContextVar("delta_creds", default=None)


class CredentialsMissing(RuntimeError):
    """A signed call was attempted but no Delta credentials are available for this request."""


def use_credentials(api_key: str | None, api_secret: str | None, env: str) -> contextvars.Token:
    """Bind credentials to the current context. Empty keys mean 'none' -- the process globals are NOT consulted."""
    return _CREDS.set((api_key or None, api_secret or None, env))


def _active() -> tuple[str | None, str | None, str]:
    c = _CREDS.get()
    if c is not None:
        return c[0], c[1], BASE_URLS.get(c[2], BASE_URLS["india_prod"])
    return API_KEY, API_SECRET, BASE_URL


def active_env() -> str:
    c = _CREDS.get()
    return c[2] if c is not None else ENV
```
Replace `_sign` and the credential/URL parts of `_request`:
```python
def _sign(method: str, timestamp: str, path: str, query_string: str, body: str, secret: str | None = None) -> str:
    message = method + timestamp + path + query_string + body
    return hmac.new((secret if secret is not None else API_SECRET).encode(), message.encode(), hashlib.sha256).hexdigest()
```
In `_request`: change `url = BASE_URL + path + query_string` to
```python
    api_key, api_secret, base_url = _active()
    url = base_url + path + query_string
```
and the auth block to
```python
    if auth:
        if not api_key or not api_secret:
            raise CredentialsMissing(
                "DELTA_API_KEY / DELTA_API_SECRET not set. Run "
                "`uvx delta-exchange-mcp login`, or export the env vars, or fix .env."
            )
        timestamp = str(int(time.time()))
        headers["api-key"] = api_key
        headers["timestamp"] = timestamp
        headers["signature"] = _sign(method, timestamp, path, query_string, payload, api_secret)
```
Replace `get_product_id` cache use (lines 188-192):
```python
    key = (_active()[2], symbol)
    if key not in _PRODUCT_CACHE:
        _PRODUCT_CACHE[key] = get_product(symbol)
    return _PRODUCT_CACHE[key]["id"]
```
(Keep the function's existing signature/docstring and any symbol normalisation that precedes line 190.) `CredentialsMissing` subclasses `RuntimeError`, so existing CLI error handling still catches it.

- [ ] **Step 4: Verify passing**

Run: `cd backend && .venv/bin/python -m pytest tests/test_delta_context.py -v`
Expected: 6 passed. Also `cd .. && backend/.venv/bin/python delta_api.py --help` still prints usage.

---

### Task 4: Clerk JWT verification

**Files:**
- Create: `backend/auth.py`, `backend/tests/test_auth.py`

**Interfaces:**
- Consumes: `store.get_credentials`, `delta.use_credentials`.
- Produces:
  - `auth.issuer_from_publishable_key(pk: str) -> str` (e.g. `https://crucial-seasnail-7.clerk.accounts.dev`)
  - `auth.verify_token(token: str) -> str` returns Clerk user id (`sub`) or raises `HTTPException(401)`.
  - `auth.user_id_var: ContextVar[str | None]`
  - `async auth.bind_user(authorization: str | None = Header(None)) -> str` — FastAPI dependency: verifies, sets `user_id_var`, loads the user's creds and calls `delta.use_credentials` (empty creds if none saved).
  - Signing-key lookup isolated in `auth._signing_key(token) -> key` so tests can monkeypatch it.
  - Env: `CLERK_PUBLISHABLE_KEY` (or `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`), read at call time.

- [ ] **Step 1: Write failing tests**

`backend/tests/test_auth.py`:
```python
import time
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

PK = "pk_test_Y3J1Y2lhbC1zZWFzbmFpbC03LmNsZXJrLmFjY291bnRzLmRldiQ"
ISS = "https://crucial-seasnail-7.clerk.accounts.dev"


@pytest.fixture
def keys(monkeypatch):
    import auth
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setenv("CLERK_PUBLISHABLE_KEY", PK)
    monkeypatch.setattr(auth, "_signing_key", lambda token: priv.public_key())
    return priv


def mint(priv, **over):
    claims = {"sub": "user_1", "iss": ISS, "exp": int(time.time()) + 60, "iat": int(time.time()) - 1, **over}
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, priv, algorithm="RS256")


def test_issuer_from_publishable_key():
    import auth
    assert auth.issuer_from_publishable_key(PK) == ISS


def test_valid_token(keys):
    import auth
    assert auth.verify_token(mint(keys)) == "user_1"


@pytest.mark.parametrize("over", [{"exp": 1}, {"iss": "https://evil.example"}, {"sub": None}])
def test_rejected_claims(keys, over):
    import auth
    with pytest.raises(HTTPException) as e:
        auth.verify_token(mint(keys, **over))
    assert e.value.status_code == 401


def test_rejects_hs256_and_garbage(keys):
    import auth
    hs = jwt.encode({"sub": "u", "iss": ISS, "exp": int(time.time()) + 60}, "x" * 40, algorithm="HS256")
    for bad in (hs, "not.a.jwt", ""):
        with pytest.raises(HTTPException) as e:
            auth.verify_token(bad)
        assert e.value.status_code == 401


def test_rejects_wrong_signing_key(keys, monkeypatch):
    import auth
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(HTTPException):
        auth.verify_token(mint(other))
```

- [ ] **Step 2: Run, expect FAIL** (`ModuleNotFoundError: auth`):
`cd backend && .venv/bin/python -m pytest tests/test_auth.py -v`

- [ ] **Step 3: Implement `backend/auth.py`**

```python
"""Clerk session-token verification and per-request Delta credential binding."""

from __future__ import annotations

import base64
import contextvars
import os
from functools import lru_cache

import jwt
from fastapi import Header, HTTPException
from jwt import PyJWKClient

import delta_api as delta
import store

user_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("user_id", default=None)


def _pk() -> str:
    pk = os.environ.get("CLERK_PUBLISHABLE_KEY") or os.environ.get("NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY") or ""
    if not pk:
        raise HTTPException(500, "CLERK_PUBLISHABLE_KEY is not configured on the backend")
    return pk


def issuer_from_publishable_key(pk: str) -> str:
    b64 = pk.split("_", 2)[2]
    domain = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)).decode().rstrip("$")
    return f"https://{domain}"


@lru_cache(maxsize=4)
def _jwks(issuer: str) -> PyJWKClient:
    return PyJWKClient(f"{issuer}/.well-known/jwks.json", cache_keys=True)


def _signing_key(token: str):
    return _jwks(issuer_from_publishable_key(_pk())).get_signing_key_from_jwt(token).key


def verify_token(token: str) -> str:
    try:
        issuer = issuer_from_publishable_key(_pk())
        claims = jwt.decode(token, _signing_key(token), algorithms=["RS256"], issuer=issuer,
                            options={"require": ["exp", "iss", "sub"]}, leeway=5)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(401, "Invalid or expired session", headers={"WWW-Authenticate": "Bearer"}) from None
    sub = claims.get("sub")
    if not sub:
        raise HTTPException(401, "Invalid session")
    return str(sub)


async def bind_user(authorization: str | None = Header(None)) -> str:
    """Runs in the request's task context, so the ContextVars set here are copied into the threadpool endpoint."""
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "Missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    uid = verify_token(token)
    user_id_var.set(uid)
    creds = store.get_credentials(uid)
    delta.use_credentials(creds.api_key if creds else None, creds.api_secret if creds else None, creds.env if creds else "india_prod")
    return uid
```

- [ ] **Step 4: Verify passing**

Run: `cd backend && .venv/bin/python -m pytest tests/test_auth.py -v`
Expected: all pass (the garbage-token cases are caught by the broad `except` and map to 401).

---

### Task 5: Wire `main.py` — auth, 412, settings endpoints, scoped audit

**Files:**
- Modify: `backend/main.py` (docstring, lines 17-117 auth/app/audited, `status`, `auth_check`, `history`, lines ~420-430 static mount)
- Delete: `backend/db.py` (after tests pass)
- Create: `backend/tests/test_api.py`

**Interfaces:**
- Consumes: `auth.bind_user`, `auth.user_id_var`, `store.*`, `delta.CredentialsMissing`, `delta.use_credentials`.
- Produces endpoints: `GET /settings/delta -> {"is_set": bool, "env": str|None, "api_key_masked": str|None}`; `PUT /settings/delta` body `{"api_key": str, "api_secret": str, "env": "india_prod"|"india_testnet"} -> same shape as GET`; `DELETE /settings/delta -> {"is_set": false}`.

- [ ] **Step 1: Write failing tests**

`backend/tests/test_api.py`:
```python
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
```

- [ ] **Step 2: Run, expect FAIL** (`/settings/delta` 404 / old API-key auth):
`cd backend && .venv/bin/python -m pytest tests/test_api.py -v`

- [ ] **Step 3: Edit `backend/main.py`**

3a. Replace the module docstring run instructions with:
```
Every endpoint requires  Authorization: Bearer <Clerk session token>  (the Next.js frontend attaches it).
Set CLERK_PUBLISHABLE_KEY in backend/.env. Each user's Delta keys are saved via PUT /settings/delta.
Run:  .venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000
NOTE: order endpoints place REAL orders on the calling user's Delta account.
```
3b. Replace imports and the whole "Auth" block (everything from `import functools` imports through `require_api_key` and the `app = FastAPI(...)` call) with:
```python
import functools
import sys
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv(Path(__file__).resolve().parent / ".env")
import auth  # noqa: E402
import delta_api as delta  # noqa: E402
import store  # noqa: E402

app = FastAPI(
    title="Delta Exchange India Backend",
    description="REST wrapper over delta_api.py. Authenticate with a Clerk session token (`Authorization: Bearer ...`).",
    version="2.0.0",
    dependencies=[Depends(auth.bind_user)],
)
```
(Remove `secrets`, `os`, `RedirectResponse`, `StaticFiles`, `APIKeyHeader`, `_load_backend_key`, `BACKEND_API_KEY`, `require_api_key`.)

3c. Add the 412 handler *above* the `RuntimeError` handler (Starlette picks the most specific class, but keep it explicit):
```python
@app.exception_handler(delta.CredentialsMissing)
async def _creds_missing(_: Request, exc: delta.CredentialsMissing):
    return JSONResponse(status_code=412, content={"detail": "delta_credentials_missing"})
```
3d. In `audited`, replace the two `db.log_event(action, req, ...)` calls with `store.log_event(auth.user_id_var.get() or "unknown", action, req, ...)`.

3e. Replace `auth_check`, `status`, and `history` bodies:
```python
@app.get("/auth/check", tags=["meta"], summary="Validate the session token (no Delta call)")
def auth_check() -> dict[str, bool]:
    return {"ok": True}


@app.get("/status", tags=["meta"], summary="Environment, credentials and account sanity check")
def status() -> dict[str, Any]:
    return {"env": delta.active_env(), "delta_credentials_set": store.get_credentials(auth.user_id_var.get()) is not None,
            "balances": delta.get_balances(), "open_orders": delta.get_open_orders()}


@app.get("/history", tags=["meta"], summary="Audit log of the caller's actions")
def history(limit: int = Query(100, ge=1, le=1000), action: str | None = None):
    return store.list_events(auth.user_id_var.get(), limit, action)
```
3f. Add the settings endpoints (before the Web UI section) and delete the Web UI section (`_root`, `add_route`, `mount`):
```python
class DeltaCredsIn(BaseModel):
    api_key: str = Field(min_length=4)
    api_secret: str = Field(min_length=4)
    env: Literal["india_prod", "india_testnet"] = "india_prod"


def _creds_view(uid: str) -> dict[str, Any]:
    c = store.get_credentials(uid)
    return {"is_set": True, "env": c.env, "api_key_masked": "••••" + c.api_key[-4:]} if c else {"is_set": False, "env": None, "api_key_masked": None}


@app.get("/settings/delta", tags=["settings"], summary="Whether Delta keys are saved (never returns the secret)")
def get_delta_settings():
    return _creds_view(auth.user_id_var.get())


@app.put("/settings/delta", tags=["settings"], summary="Validate and save the caller's Delta API keys")
def put_delta_settings(body: DeltaCredsIn):
    uid = auth.user_id_var.get()
    tok = delta.use_credentials(body.api_key.strip(), body.api_secret.strip(), body.env)
    try:
        delta.get_balances()                       # cheap authenticated call proves the keys work for this env
    except delta.DeltaAPIError as exc:
        raise HTTPException(400, f"Delta rejected these credentials ({body.env}): {exc.payload}") from None
    finally:
        delta._CREDS.reset(tok)
    store.save_credentials(uid, body.api_key.strip(), body.api_secret.strip(), body.env)
    return _creds_view(uid)


@app.delete("/settings/delta", tags=["settings"], summary="Remove the caller's saved Delta keys")
def delete_delta_settings():
    store.delete_credentials(auth.user_id_var.get())
    return {"is_set": False}
```
Note: `PUT` is a sync endpoint (threadpool); `_CREDS.reset(tok)` runs in the same thread context that set it, so it is valid.

- [ ] **Step 4: Verify passing**

Run: `cd backend && .venv/bin/python -m pytest -v`
Expected: all tests in all files pass. If `test_concurrent_requests_do_not_leak_keys` is flaky-free across 3 runs, proceed.

- [ ] **Step 5: Remove `db.py`, smoke-run, and add the Clerk key to `.env`**

```bash
cd /home/anand-shaurya/Documents/personal/delta/backend
rm db.py
printf 'CLERK_PUBLISHABLE_KEY=pk_test_Y3J1Y2lhbC1zZWFzbmFpbC03LmNsZXJrLmFjY291bnRzLmRldiQ\n' >> .env
sed -i '/^BACKEND_API_KEY=/d' .env; chmod 600 .env
.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000 &
sleep 2; curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/balances   # expect 401
kill %1
```
Expected: `401`. (The publishable key is public by design; the secret key is only needed by the frontend.)

---

### Task 6: Frontend scaffold, Clerk and API proxy

**Files:**
- Create under `frontend/`: `package.json`, `tsconfig.json`, `next.config.ts`, `.env.local`, `.gitignore`, `middleware.ts`, `app/layout.tsx`, `app/globals.css`, `app/providers.tsx`, `app/sign-in/[[...sign-in]]/page.tsx`, `app/api/[...path]/route.ts`, `lib/proxy.ts`, `lib/proxy.test.ts`

**Interfaces:**
- Produces: `buildBackendUrl(path: string[], search: string, base: string): string`; route handler forwarding any method to `${BACKEND_URL}/${path}` with `Authorization: Bearer <clerk token>`, returning backend status and body unchanged; `401` JSON when signed out.

- [ ] **Step 1: Scaffold**

```bash
cd /home/anand-shaurya/Documents/personal/delta
mkdir frontend && cd frontend
cat > package.json <<'EOF'
{
  "name": "delta-frontend",
  "private": true,
  "scripts": { "dev": "next dev -p 3000", "build": "next build", "start": "next start -p 3000", "typecheck": "tsc --noEmit", "test": "vitest run" }
}
EOF
npm i next@15 react@19 react-dom@19 @clerk/nextjs@6 @tanstack/react-query@5
npm i -D typescript @types/node @types/react @types/react-dom vitest
```
`tsconfig.json`:
```json
{ "compilerOptions": { "target": "ES2022", "lib": ["dom", "dom.iterable", "esnext"], "allowJs": false, "skipLibCheck": true, "strict": true, "noEmit": true, "esModuleInterop": true, "module": "esnext", "moduleResolution": "bundler", "resolveJsonModule": true, "isolatedModules": true, "jsx": "preserve", "incremental": true, "plugins": [{ "name": "next" }], "paths": { "@/*": ["./*"] } }, "include": ["next-env.d.ts", "**/*.ts", "**/*.tsx", ".next/types/**/*.ts"], "exclude": ["node_modules"] }
```
`next.config.ts`: `import type { NextConfig } from "next"; const config: NextConfig = {}; export default config;`
`.gitignore`: `node_modules`, `.next`, `.env*.local`, `next-env.d.ts`.
`.env.local` (mode 600; values from the user's message, **do not echo them in output**):
```
NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=pk_test_Y3J1Y2lhbC1zZWFzbmFpbC03LmNsZXJrLmFjY291bnRzLmRldiQ
CLERK_SECRET_KEY=<the sk_test_ value the user supplied>
NEXT_PUBLIC_CLERK_SIGN_IN_URL=/sign-in
BACKEND_URL=http://127.0.0.1:8000
```

- [ ] **Step 2: Failing test for URL building**

`frontend/lib/proxy.test.ts`:
```ts
import { describe, expect, it } from "vitest";
import { buildBackendUrl } from "./proxy";

describe("buildBackendUrl", () => {
  it("joins path segments and keeps the query string", () => {
    expect(buildBackendUrl(["orders", "BTCUSD", "12"], "?a=1", "http://x:8000")).toBe("http://x:8000/orders/BTCUSD/12?a=1");
  });
  it("encodes segments and refuses path traversal", () => {
    expect(buildBackendUrl(["a b"], "", "http://x")).toBe("http://x/a%20b");
    expect(() => buildBackendUrl(["..", "settings"], "", "http://x")).toThrow();
    expect(() => buildBackendUrl(["a", "%2e%2e"], "", "http://x")).toThrow();
  });
});
```
Run: `cd frontend && npx vitest run` -> FAIL (module missing).

- [ ] **Step 3: Implement**

`frontend/lib/proxy.ts`:
```ts
export function buildBackendUrl(path: string[], search: string, base: string): string {
  const segs = path.map((s) => {
    const d = decodeURIComponent(s);
    if (d === ".." || d === "." || d.includes("/")) throw new Error("bad path");
    return encodeURIComponent(d);
  });
  return `${base.replace(/\/$/, "")}/${segs.join("/")}${search}`;
}
```
`frontend/middleware.ts`:
```ts
import { clerkMiddleware, createRouteMatcher } from "@clerk/nextjs/server";

const isPublic = createRouteMatcher(["/sign-in(.*)"]);

export default clerkMiddleware(async (auth, req) => {
  if (!isPublic(req)) await auth.protect();
});

export const config = { matcher: ["/((?!_next|.*\\..*).*)", "/(api)(.*)"] };
```
(`auth.protect()` returns 401 for `/api/*` fetches and redirects pages to sign-in.)

`frontend/app/api/[...path]/route.ts`:
```ts
import { auth } from "@clerk/nextjs/server";
import { NextRequest, NextResponse } from "next/server";
import { buildBackendUrl } from "@/lib/proxy";

const BACKEND = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

async function handler(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  const { getToken } = await auth();
  const token = await getToken();
  if (!token) return NextResponse.json({ detail: "Not signed in" }, { status: 401 });
  const { path } = await ctx.params;
  let url: string;
  try { url = buildBackendUrl(path, req.nextUrl.search, BACKEND); } catch { return NextResponse.json({ detail: "Bad path" }, { status: 400 }); }
  const hasBody = !["GET", "HEAD"].includes(req.method);
  try {
    const res = await fetch(url, {
      method: req.method,
      headers: { Authorization: `Bearer ${token}`, ...(hasBody ? { "Content-Type": "application/json" } : {}) },
      body: hasBody ? await req.text() : undefined,
      cache: "no-store",
    });
    return new NextResponse(await res.text(), { status: res.status, headers: { "Content-Type": res.headers.get("content-type") ?? "application/json" } });
  } catch {
    return NextResponse.json({ detail: "Backend unreachable" }, { status: 503 });
  }
}
export { handler as GET, handler as POST, handler as PUT, handler as DELETE };
```
`frontend/app/layout.tsx`:
```tsx
import { ClerkProvider } from "@clerk/nextjs";
import type { ReactNode } from "react";
import "./globals.css";
import { Providers } from "./providers";

export const metadata = { title: "Delta Trader" };
export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <ClerkProvider>
      <html lang="en"><body><Providers>{children}</Providers></body></html>
    </ClerkProvider>
  );
}
```
`frontend/app/sign-in/[[...sign-in]]/page.tsx`:
```tsx
import { SignIn } from "@clerk/nextjs";
export default function Page() {
  return <div style={{ display: "grid", placeItems: "center", minHeight: "100vh" }}><SignIn /></div>;
}
```
`frontend/app/globals.css`: copy the `<style>` block from `backend/static/index.html` lines 8-74 verbatim (CSS variables, table.term, chips, tags, stat cards, `.marked` row highlight). Add `.hidden{display:none}` if not present.
`frontend/app/providers.tsx` is created in Task 7 (a stub `export function Providers({children}:{children:React.ReactNode}){return <>{children}</>}` is fine until then).

- [ ] **Step 4: Verify**

Run: `cd frontend && npx vitest run && npx tsc --noEmit && npx next build`
Expected: tests pass, typecheck clean, build succeeds. Then start both servers (`backend/.venv/bin/uvicorn` on 8000, `npm run dev` on 3000), open `http://localhost:3000`, confirm redirect to the Clerk sign-in page and `curl -i localhost:3000/api/balances` returns 401.

---

### Task 7: API client, query provider, app shell, 401/412 handling

**Files:**
- Create: `frontend/lib/api.ts`, `frontend/lib/format.ts`, `frontend/lib/api.test.ts`, `frontend/app/providers.tsx` (replace stub), `frontend/app/(app)/layout.tsx`, `frontend/app/(app)/page.tsx`, `frontend/components/Nav.tsx`, `frontend/components/Toast.tsx`, `frontend/components/ConfirmDialog.tsx`, `frontend/components/Ui.tsx`

**Interfaces:**
- Produces:
  - `api<T>(method: string, path: string, body?: unknown): Promise<T>` — calls `/api${path}`; throws `ApiError{status:number; detail:string}`; on 401 `window.location.assign("/sign-in")`; on 412 `window.location.assign("/settings?missing=1")`.
  - `errorMessage(status: number, data: any): string` — the same formatting as the old UI: `delta_error` JSON if present, else `detail` string, else JSON.
  - `num(v, d=2)`, `signClass(v)` from `lib/format.ts` (port of the old `num`/`sign`).
  - `useToast(): (msg: string, kind?: "ok" | "err") => void`, `useConfirm(): (text: string) => Promise<boolean>`.
  - `<Stat label value cls?/>`, `<Tag text cls/>`, `<Chip on onClick/>` in `Ui.tsx`.
  - Symbol state: `useSymbol(): [string, (s: string) => void]` persisted in `localStorage` (default `BTCUSD`), shared via context in `(app)/layout.tsx`.

- [ ] **Step 1: Write failing tests** (`frontend/lib/api.test.ts`)

```ts
import { describe, expect, it, vi, beforeEach } from "vitest";
import { api, ApiError, errorMessage } from "./api";
import { num } from "./format";

describe("errorMessage", () => {
  it("prefers delta_error then detail", () => {
    expect(errorMessage(502, { delta_error: { code: "x" } })).toContain('"code":"x"');
    expect(errorMessage(400, { detail: "bad" })).toBe("400: bad");
  });
});
describe("num", () => {
  it("formats and handles empties", () => { expect(num(1234.5)).toBe("1,234.50"); expect(num(null)).toBe("–"); expect(num("abc")).toBe("–"); });
});
describe("api", () => {
  beforeEach(() => { (globalThis as any).window = { location: { assign: vi.fn() } }; });
  it("redirects on 401 and 412 and throws ApiError", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({ ok: false, status: 412, json: async () => ({ detail: "delta_credentials_missing" }) }) as any;
    await expect(api("GET", "/balances")).rejects.toBeInstanceOf(ApiError);
    expect((window as any).location.assign).toHaveBeenCalledWith("/settings?missing=1");
    globalThis.fetch = vi.fn().mockResolvedValue({ ok: false, status: 401, json: async () => ({}) }) as any;
    await expect(api("GET", "/x")).rejects.toBeInstanceOf(ApiError);
    expect((window as any).location.assign).toHaveBeenCalledWith("/sign-in");
  });
  it("sends JSON body and returns parsed data", async () => {
    const f = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ a: 1 }) });
    globalThis.fetch = f as any;
    expect(await api("PUT", "/leverage/BTCUSD", { leverage: 5 })).toEqual({ a: 1 });
    expect(f).toHaveBeenCalledWith("/api/leverage/BTCUSD", expect.objectContaining({ method: "PUT", body: '{"leverage":5}' }));
  });
});
```
Run `npx vitest run` -> FAIL.

- [ ] **Step 2: Implement**

`frontend/lib/format.ts`:
```ts
export const num = (v: unknown, d = 2): string =>
  v == null || v === "" || isNaN(+(v as number)) ? "–" : (+(v as number)).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
export const signClass = (v: number): "g" | "r" | "d" => (v > 0 ? "g" : v < 0 ? "r" : "d");
```
`frontend/lib/api.ts`:
```ts
export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}
export function errorMessage(status: number, data: any): string {
  const d = data?.delta_error ? JSON.stringify(data.delta_error) : typeof data?.detail === "string" ? data.detail : JSON.stringify(data?.detail ?? data);
  return `${status}: ${d}`;
}
export async function api<T = any>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`/api${path}`, { method, headers: body !== undefined ? { "Content-Type": "application/json" } : undefined, body: body !== undefined ? JSON.stringify(body) : undefined });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) { window.location.assign("/sign-in"); throw new ApiError(401, "Session expired"); }
  if (res.status === 412) { window.location.assign("/settings?missing=1"); throw new ApiError(412, "Add your Delta API keys in Settings"); }
  if (!res.ok) throw new ApiError(res.status, errorMessage(res.status, data));
  return data as T;
}
```
`providers.tsx`: `"use client"`; `QueryClientProvider` (defaultOptions `queries: { retry: false, refetchOnWindowFocus: false }`), `ToastProvider`, `ConfirmProvider`. `Toast.tsx`: a fixed-position `#toast` div styled by existing CSS (ok = green border, err = red, errors persist 8s). `ConfirmDialog.tsx`: `<dialog>` + `useConfirm()` returning a promise (port of old `confirmDlg`, `index.html` ~line 325).
`(app)/layout.tsx`: client component with header: `<UserButton/>` from Clerk, symbol input (context), live price (`useQuery(["price", sym], () => api("GET", "/price/"+sym), {refetchInterval: 5000})`), `Nav`. `Nav.tsx` links: Dashboard `/`, Trade `/trade`, Orders `/orders`, Analysis `/analysis`, History `/history`, Settings `/settings`; active link gets class `on` (reuse the old `nav button.on` CSS via `nav a`).
`(app)/page.tsx`: placeholder `<h2>Dashboard</h2>` (replaced in Task 11).

- [ ] **Step 3: Verify**

Run: `cd frontend && npx vitest run && npx tsc --noEmit`
Expected: all pass. Manual: signed in, nav renders, header price updates (price will 412 -> redirects to Settings until keys are saved, which is the intended flow).

---

### Task 8: Settings page (Delta API keys)

**Files:**
- Create: `frontend/app/(app)/settings/page.tsx`, `frontend/lib/settings.ts`, `frontend/lib/settings.test.ts`

**Interfaces:**
- Consumes: `api`, `useToast`, `useConfirm`, backend `GET/PUT/DELETE /settings/delta`.
- Produces: `type DeltaSettings = { is_set: boolean; env: "india_prod" | "india_testnet" | null; api_key_masked: string | null }`; `validateCredsForm(f: {apiKey: string; apiSecret: string}): string | null`.

- [ ] **Step 1: Failing test** (`lib/settings.test.ts`)

```ts
import { expect, it } from "vitest";
import { validateCredsForm } from "./settings";
it("validates", () => {
  expect(validateCredsForm({ apiKey: "", apiSecret: "x".repeat(8) })).toMatch(/key/i);
  expect(validateCredsForm({ apiKey: "abcd1234", apiSecret: "  " })).toMatch(/secret/i);
  expect(validateCredsForm({ apiKey: " abcd1234 ", apiSecret: "secret99" })).toBeNull();
});
```
Run -> FAIL.

- [ ] **Step 2: Implement**

`lib/settings.ts`:
```ts
export type DeltaSettings = { is_set: boolean; env: "india_prod" | "india_testnet" | null; api_key_masked: string | null };
export function validateCredsForm(f: { apiKey: string; apiSecret: string }): string | null {
  if (f.apiKey.trim().length < 4) return "Enter your Delta API key.";
  if (f.apiSecret.trim().length < 4) return "Enter your Delta API secret.";
  return null;
}
```
`settings/page.tsx` (client component):
- `useQuery(["delta-settings"], () => api<DeltaSettings>("GET", "/settings/delta"))`.
- Status panel: "Connected: key ••••1234 on india_prod" or "Not set"; if `?missing=1` show a warning "Add your Delta API keys to continue."
- Form: API key (text, `autoComplete="off"`), API secret (`type="password"`, `autoComplete="new-password"`, never pre-filled), Environment `<select>` (`india_prod` = "India production", `india_testnet` = "India testnet"). **Save** runs `validateCredsForm`, then `api("PUT", "/settings/delta", {api_key, api_secret, env})`; success -> toast "Keys verified and saved", clear the secret field, invalidate `["delta-settings"]`; failure -> error toast with the backend's message (shows Delta's rejection reason).
- **Remove keys** button (only when set) with confirm -> `DELETE`.
- Note text: "Your secret is encrypted on the server and never shown again."
- Layout uses the existing `section`, `label`, `input`, `button.btn` styles.

- [ ] **Step 3: Verify**

Run: `npx vitest run && npx tsc --noEmit && npx next build`. Manual: save a wrong key -> error toast and `GET /settings/delta` still `is_set:false`; save the real testnet or prod keys -> status shows masked key; reload keeps them; Remove clears them. Check `backend/data.db` contains no plaintext key (`strings data.db | grep -c <key>` is 0).

---

### Task 9: Analysis page (with timeframe tick-highlight)

**Files:**
- Create: `frontend/lib/analysis.ts`, `frontend/lib/analysis.test.ts`, `frontend/app/(app)/analysis/page.tsx`, `frontend/components/AnalysisTable.tsx`

**Interfaces:**
- Consumes: `api`, `num`, `useSymbol`, `Stat`, `Tag`, `Chip`.
- Produces:
  - `TFS: readonly ["5s","1m","3m","5m","15m","30m","1h","2h","4h","6h","1d","1w"]`
  - `markColor(res: string): string` — `MARK_COLORS[TFS.indexOf(res) % 12]`, with `MARK_COLORS = ['#f59e0b','#22d3ee','#c084fc','#f472b6','#a3e635','#fb7185','#60a5fa','#34d399','#fbbf24','#e879f9','#2dd4bf','#f87171']`
  - `toggleSelected(sel: string[], t: string): string[]` (keeps TFS order)
  - `pushSnapshot(state: {snaps: Snap[]; idx: number; key: string}, key: string, data: any, now: number, max = 10): State` — resets on key change, only auto-advances if idx was on latest, caps at `max`.
  - `type Snap = { time: number; data: AnalysisResponse }`.

- [ ] **Step 1: Failing tests** (`lib/analysis.test.ts`)

```ts
import { describe, expect, it } from "vitest";
import { TFS, markColor, toggleSelected, pushSnapshot } from "./analysis";

it("colours are stable per timeframe and distinct", () => {
  expect(markColor("15m")).toBe(markColor("15m"));
  expect(new Set(TFS.map(markColor)).size).toBe(TFS.length);
});
it("toggleSelected keeps canonical order", () => {
  expect(toggleSelected(["1h", "5m"], "15m")).toEqual(["5m", "15m", "1h"]);
  expect(toggleSelected(["5m", "1h"], "5m")).toEqual(["1h"]);
});
describe("pushSnapshot", () => {
  const s0 = { snaps: [], idx: -1, key: "" };
  it("follows latest and caps at 10", () => {
    let s = s0 as any;
    for (let i = 0; i < 12; i++) s = pushSnapshot(s, "k", { i }, i);
    expect(s.snaps.length).toBe(10); expect(s.idx).toBe(9); expect(s.snaps[0].data.i).toBe(2);
  });
  it("does not jump when viewing an older snapshot", () => {
    let s = s0 as any;
    for (let i = 0; i < 3; i++) s = pushSnapshot(s, "k", { i }, i);
    s = { ...s, idx: 0 };
    s = pushSnapshot(s, "k", { i: 3 }, 3);
    expect(s.idx).toBe(0);
  });
  it("resets when the key changes", () => {
    let s = pushSnapshot(s0 as any, "a", {}, 0); s = pushSnapshot(s, "b", {}, 1);
    expect(s.snaps.length).toBe(1); expect(s.key).toBe("b");
  });
});
```
Run -> FAIL.

- [ ] **Step 2: Implement `lib/analysis.ts`** (logic ported from `backend/static/index.html` lines ~593-690)

```ts
export const TFS = ["5s","1m","3m","5m","15m","30m","1h","2h","4h","6h","1d","1w"] as const;
export const MARK_COLORS = ['#f59e0b','#22d3ee','#c084fc','#f472b6','#a3e635','#fb7185','#60a5fa','#34d399','#fbbf24','#e879f9','#2dd4bf','#f87171'];
export const markColor = (res: string) => MARK_COLORS[Math.max(0, TFS.indexOf(res as any)) % MARK_COLORS.length];
export const toggleSelected = (sel: string[], t: string) => (sel.includes(t) ? sel.filter((x) => x !== t) : TFS.filter((x) => sel.includes(x) || x === t));

export type Snap = { time: number; data: any };
export type SnapState = { snaps: Snap[]; idx: number; key: string };
export function pushSnapshot(st: SnapState, key: string, data: any, now: number, max = 10): SnapState {
  let { snaps, idx } = key !== st.key ? { snaps: [] as Snap[], idx: -1 } : st;
  const follow = idx === snaps.length - 1;
  snaps = [...snaps, { time: now, data }];
  if (snaps.length > max) { snaps = snaps.slice(1); if (!follow) idx = Math.max(0, idx - 1); }
  if (follow) idx = snaps.length - 1;
  return { snaps, idx, key };
}
```
- [ ] **Step 3: Implement the page and table**

`AnalysisTable.tsx`: renders the `table.term` with columns `Timeframe, ADX, RSI, ATR, Zone, Strength, Direction, Structure, DI+, DI-, Volume, Vol Avg, Vol Ratio`. Row rules (identical to old code): zone tag `r` for overbought, `g` for oversold, else `d`; strength `trending` (g) / `ranging` (w); direction `UP` g / `DOWN` r; structure `g` for HH/HL, `r` for LH/LL, else `d`; DI+ is `g` when `plus_di >= minus_di` else `d`, DI- is `r` when the other way; vol-ratio `g` when `>= 1.5`; errored rows show `error: ...` spanning all columns. First cell: `<label><input type="checkbox" checked={marked.includes(res)} onChange=.../> {res}</label>`; the `<tr>` gets `className={marked ? "marked" : ""}` and `style={{"--mc": markColor(res)} as React.CSSProperties}`. `marked` is persisted in `localStorage["aMark"]` (try/catch; render without it on failure).

`analysis/page.tsx`:
- State: selected timeframes (`localStorage["aSel"]`, default `["15m","1h","4h","1d"]`), params `period=14, threshold=25, overbought=70, oversold=30`, `autoRefresh` (default on) + interval select (persisted `aEvery`), `SnapState`.
- Chips row with TFS chips and an All/Clear chip.
- Fetch: `api("GET", /analysis/${sym}?period=..&threshold=..&overbought=..&oversold=..&resolutions=15m&resolutions=1h...)`; guard re-entrancy (ignore if already loading); toast "Pick at least one timeframe" when none selected. Push into `SnapState` via `pushSnapshot(state, key, data, Date.now())` where `key = sym + "?" + query`.
- Stats strip: Symbol, Price (with ▲/▼ delta vs the previous snapshot, coloured), Leverage, Max contracts, 24h volume, Open interest (contracts and USD), OI change 6h — same formulas as the old `renderAnalysis`.
- Snapshot navigation: prev/next/latest buttons, `n / total`, dots, stamp "live · updated HH:MM:SS" or "snapshot from ... (Ns ago)"; ArrowLeft/ArrowRight keys when no input is focused.
- Auto-refresh: `setInterval` while the tab is visible (`!document.hidden`).

- [ ] **Step 4: Verify**

Run: `npx vitest run && npx tsc --noEmit && npx next build`. Manual: with saved keys, `/analysis` loads the table; ticking 15m and 1h highlights each row in a different colour and survives auto-refresh and reload; prev/next browse snapshots.

---

### Task 10: Orders and History pages

**Files:**
- Create: `frontend/app/(app)/orders/page.tsx`, `frontend/app/(app)/history/page.tsx`, `frontend/components/ProtectDialog.tsx`, `frontend/components/TradeDetail.tsx`, `frontend/lib/history.ts`, `frontend/lib/history.test.ts`

**Interfaces:**
- Consumes: `api`, `useConfirm`, `useToast`, `num`, `Stat`, `Tag`, `useSymbol`.
- Produces: `pnlOf(o): number | null`, `kindOf(o): string`, `sortTrades(rows, key, dir)` in `lib/history.ts` — extracted from the old helpers (`backend/static/index.html` lines ~429-460 `pnlOf`/`kindOf`/last-trade logic, ~729-908 trades/timeline).

Port map (read the cited old code and reproduce behaviour exactly; endpoints unchanged):

| Page | Old code | Endpoints |
|---|---|---|
| Orders | `index.html` 545-592 (`loadOrders`, cancel one/all, trailing cancel, protect form) | `GET /orders`, `DELETE /orders/{symbol}/{id}`, `DELETE /orders[?symbol=]`, `POST /orders/{stop-loss,take-profit,trailing-stop}`, `DELETE /orders/{symbol}/trailing-stop`, `PUT /orders/bracket` |
| Protect dialog | `index.html` 391-428 (`openProtect`) | `PUT /orders/bracket`, `POST /orders/stop-loss` etc. |
| History | `index.html` 684-908: three views (Trades & P&L, Actions log, Combined timeline), trade detail dialog, P&L chart (SVG) | `GET /history?limit=1000`, `GET /orders/history?limit=200`, `GET /fills?limit=200` |

- [ ] **Step 1: Failing tests for the pure helpers** — copy the exact `pnlOf`/`kindOf` logic from the old file into `lib/history.ts`, then write `lib/history.test.ts` with: an order whose `meta_data.pnl` is `"12.5"` returns `12.5`; one without returns `null`; `kindOf` returns the same labels as the old UI for market/limit/stop/take-profit/trailing/bracket orders (derive the expected labels by reading `kindOf` in the old file); `sortTrades` sorts by `time` desc and by `pnl` asc and is stable for ties. Run `npx vitest run` -> FAIL before the helpers exist, PASS after.

- [ ] **Step 2: Implement the pages**, converting each `el(...)` DOM builder into JSX one-for-one: same columns, same tag colours, same confirm texts, same toast messages. Use `useQuery` for loads and `useMutation` + `invalidateQueries` after cancels/placements. History view toggle state in `useState`; the SVG P&L chart becomes a `<svg>` component with the same scales and gridlines as `index.html` lines 729-760. Auto-refresh history every 30 s if the old code does (`hTimer`).

- [ ] **Step 3: Verify**

Run: `npx vitest run && npx tsc --noEmit && npx next build`. Manual with the testnet account: place a limit order far from market (Task 11 page, or `curl` through the proxy), see it in Orders, add SL/TP via the dialog, cancel it, and see the actions in History. Confirm another Clerk user (a second sign-up) sees an empty History.

---

### Task 11: Trade and Dashboard pages

**Files:**
- Create: `frontend/app/(app)/trade/page.tsx`, replace `frontend/app/(app)/page.tsx`, `frontend/components/PositionsTable.tsx`, `frontend/lib/trade.ts`, `frontend/lib/trade.test.ts`

**Interfaces:**
- Produces: `buildOrderBody(f: {symbol: string; size: string; price: string; sl: string; tp: string}): {symbol: string; size: number; price: number | null; stop_loss: number | null; take_profit: number | null}`; `describeOrder(side: "buy" | "sell", body): string`; `sizeFromPercent(maxContracts: number, pct: number): number` (`Math.max(1, Math.floor(max * pct / 100))`).

Port map: Dashboard = `index.html` 472-488 plus the positions strip/table (`posStrip`, `posTable`, `closePos`, lines ~369-390); Trade = `index.html` 489-545.

- [ ] **Step 1: Failing tests** (`lib/trade.test.ts`)

```ts
import { expect, it } from "vitest";
import { buildOrderBody, describeOrder, sizeFromPercent } from "./trade";

it("buildOrderBody truncates size and nulls blanks", () => {
  expect(buildOrderBody({ symbol: "btcusd", size: "3.9", price: "", sl: "", tp: "" })).toEqual({ symbol: "BTCUSD", size: 3, price: null, stop_loss: null, take_profit: null });
  expect(buildOrderBody({ symbol: "BTCUSD", size: "1", price: "80000", sl: "79000", tp: "82000" })).toMatchObject({ price: 80000, stop_loss: 79000, take_profit: 82000 });
});
it("describeOrder warns it is a real order", () => {
  const t = describeOrder("buy", { symbol: "BTCUSD", size: 2, price: null, stop_loss: 79000, take_profit: null });
  expect(t).toContain("BUY 2 × BTCUSD"); expect(t).toContain("at market"); expect(t).toContain("stop-loss 79000"); expect(t).toContain("REAL order");
});
it("sizeFromPercent is at least 1 whole contract", () => {
  expect(sizeFromPercent(14, 25)).toBe(3); expect(sizeFromPercent(2, 10)).toBe(1);
});
```
Run -> FAIL.

- [ ] **Step 2: Implement**
- `lib/trade.ts` per the signatures (`describeOrder` text must match the old `submitOrder` description string exactly, `index.html` ~line 495).
- Trade page: stats strip (Available USD, Leverage, Price, Margin/contract, Long/Short liq), max-contracts table with the ★ on the current leverage, size input with the over-max warning, 25/50/75/100% buttons (`sizeFromPercent`), Buy/Sell with `useConfirm(describeOrder(...))`, Close position, leverage select + Set (skips and toasts "Already at Nx" when unchanged), refresh. Endpoints: `GET /leverage/{s}`, `GET /max-contracts/{s}[?price=]`, `GET /balances?asset=USD`, `GET /price/{s}`, `PUT /leverage/{s}`, `POST /orders/{buy|sell}`, `POST /positions/{s}/close`.
- Dashboard: balances table (non-zero only, ₹ column), positions table with total unrealized P&L and Close buttons (`GET /balances`, `GET /positions`).
- Header positions strip from Task 7 gets `GET /positions` polled every 10 s.

- [ ] **Step 3: Verify**

Run: `npx vitest run && npx tsc --noEmit && npx next build`. Manual on testnet: change leverage, place and close a 1-contract market order, confirm P&L appears in History.

---

### Task 12: Retire the legacy UI, docs and end-to-end check

**Files:**
- Delete: `backend/static/` (after confirming every tab is ported)
- Create: `README.md` at `delta/` (run instructions)

- [ ] **Step 1: Parity checklist** — open the old file's tabs list and confirm each feature exists in the new app: Dashboard, Trade (incl. leverage and percent buttons), Orders (incl. SL/TP/trailing), Analysis (incl. snapshots, auto-refresh, tick-highlight), History (3 views + trade detail), Settings. Note any gap and fix it before deleting.
- [ ] **Step 2: Delete legacy UI**

```bash
rm -r /home/anand-shaurya/Documents/personal/delta/backend/static
```
- [ ] **Step 3: README** with: run the API (`cd backend && .venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000`), run the frontend (`cd frontend && npm run dev`), required env (`backend/.env`: `CLERK_PUBLISHABLE_KEY`; `frontend/.env.local`: Clerk keys, `BACKEND_URL`), the rule that Delta keys are entered per user in Settings, and tests (`backend/.venv/bin/python -m pytest`, `npx vitest run`).
- [ ] **Step 4: Full verification**

```bash
cd /home/anand-shaurya/Documents/personal/delta/backend && .venv/bin/python -m pytest -v
cd ../frontend && npx vitest run && npx tsc --noEmit && npx next build
```
Expected: all green. Then in a browser: sign in, redirected to Settings (no keys) -> save keys -> Analysis loads -> sign out -> `curl -i localhost:3000/api/balances` is 401 -> `curl -i 127.0.0.1:8000/balances` is 401. Report which parts needed the user's real Clerk login and Delta keys and were therefore verified only up to that boundary.

---

## Self-review notes

- **Spec coverage:** architecture/proxy (T6), Clerk JWT + issuer from key (T4), ContextVar credentials + 412 + no-global-fallback (T3, T5), SQLite + Fernet + per-user events + cap (T1, T2), settings endpoints (T5) and page (T8), per-tab port with Analysis first (T9-T11), old UI removal (T12), testing as specified. The spec's `/auth/check` is kept (T5).
- **Deliberate deviations from the spec:** the spec said JWKS URL from `CLERK_JWKS_URL`/publishable key; the plan derives it from the publishable key only. `status` now reports `env` but not `base_url`.
- **Known soft spot:** Tasks 10-11 describe the ports by reference to specific old-code line ranges rather than reproducing ~600 lines of JSX; the executor must read those ranges. They are mechanical translations, but they are the least pinned-down part of the plan.
