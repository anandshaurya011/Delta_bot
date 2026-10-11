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
