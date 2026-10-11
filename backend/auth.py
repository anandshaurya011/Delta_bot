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
