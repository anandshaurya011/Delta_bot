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


def test_drain_yields_heartbeat_after_timeout_with_no_new_events():
    user_id = "user_heartbeat"
    gen = ai_chat_stream.drain(user_id, timeout_s=0.05)
    event = next(gen)
    assert event == {"type": "heartbeat"}
    gen.close()


def test_clear_stale_discards_leftover_events_before_next_drain():
    user_id = "user_stale"
    ai_chat_stream.publish(user_id, {"type": "token", "text": "stale 1"})
    ai_chat_stream.publish(user_id, {"type": "token", "text": "stale 2"})
    ai_chat_stream.clear_stale(user_id)
    ai_chat_stream.publish(user_id, {"type": "token", "text": "fresh"})
    ai_chat_stream.publish(user_id, {"type": "done"})
    events = list(ai_chat_stream.drain(user_id))
    assert events == [{"type": "token", "text": "fresh"}, {"type": "done"}]
