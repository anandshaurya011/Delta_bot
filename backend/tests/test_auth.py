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
