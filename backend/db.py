"""Tiny JSON-file store for the audit log of state-changing actions (backend/data.json)."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

_PATH = Path(__file__).resolve().parent / "data.json"
_LOCK = threading.Lock()
_MAX_EVENTS = 5000


def _read() -> dict[str, Any]:
    try:
        return json.loads(_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"events": []}


def log_event(action: str, request: Any, ok: bool, result: Any) -> None:
    event = {"ts": int(time.time()), "action": action, "request": request, "ok": ok, "result": result}
    with _LOCK:
        data = _read()
        data["events"] = (data["events"] + [event])[-_MAX_EVENTS:]
        tmp = _PATH.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, default=str)
        os.replace(tmp, _PATH)


def list_events(limit: int = 100, action: str | None = None) -> list[dict[str, Any]]:
    with _LOCK:
        events = _read()["events"]
    if action:
        events = [e for e in events if e["action"] == action]
    return events[-limit:][::-1]
