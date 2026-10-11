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
