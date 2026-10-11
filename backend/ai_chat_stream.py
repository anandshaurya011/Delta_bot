"""Per-user, in-process SSE event delivery. Single-worker only -- a disconnected or
slow client must never block a real trade's execution, so publish() never blocks."""

from __future__ import annotations

import queue
from typing import Any, Iterator

_QUEUE_MAXSIZE = 200
_queues: dict[str, queue.Queue] = {}


def get_queue(user_id: str) -> queue.Queue:
    # setdefault (not check-then-assign) so concurrent first calls for the same
    # user_id all get the same Queue. The default Queue is built eagerly and
    # discarded if the key already exists; that allocation is cheap.
    return _queues.setdefault(user_id, queue.Queue(maxsize=_QUEUE_MAXSIZE))


def publish(user_id: str, event: dict[str, Any]) -> None:
    q = get_queue(user_id)
    try:
        q.put_nowait(event)
    except queue.Full:
        try:
            q.get_nowait()
        except queue.Empty:
            pass
        q.put_nowait(event)


def drain(user_id: str, timeout_s: float = 30) -> Iterator[dict[str, Any]]:
    q = get_queue(user_id)
    while True:
        try:
            event = q.get(timeout=timeout_s)
        except queue.Empty:
            yield {"type": "heartbeat"}
            continue
        yield event
        if event.get("type") in ("done", "error"):
            return


def clear_stale(user_id: str) -> None:
    """Drop any leftover events sitting in a user's queue from a previous,
    incompletely-consumed stream, so a fresh stream request starts clean."""
    q = get_queue(user_id)
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            return
