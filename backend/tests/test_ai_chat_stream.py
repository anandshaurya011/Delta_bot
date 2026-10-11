# backend/tests/test_ai_chat_stream.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ai_chat_stream


def test_get_queue_returns_same_object_for_same_user():
    q1 = ai_chat_stream.get_queue("user_a")
    q2 = ai_chat_stream.get_queue("user_a")
    assert q1 is q2


def test_publish_and_drain_yields_events_in_order_until_done():
    user_id = "user_b"
    ai_chat_stream.publish(user_id, {"type": "token", "text": "hi"})
    ai_chat_stream.publish(user_id, {"type": "done"})
    events = list(ai_chat_stream.drain(user_id))
    assert events == [{"type": "token", "text": "hi"}, {"type": "done"}]


def test_publish_drops_oldest_when_queue_full():
    user_id = "user_c"
    for i in range(205):
        ai_chat_stream.publish(user_id, {"type": "token", "text": str(i)})
    ai_chat_stream.publish(user_id, {"type": "done"})
    events = list(ai_chat_stream.drain(user_id))
    assert events[-1] == {"type": "done"}
    assert len(events) <= 201  # bounded, did not grow unbounded or block
