"""Event schema + determinism tests."""
from __future__ import annotations

import pytest

from token_roi.events import EventType, Event, make_event, PayloadError


def test_event_id_is_deterministic():
    """Same inputs → same id, across processes."""
    kwargs = dict(
        session_id="sess01", seq=3, type=EventType.USER_PROMPT,
        payload={"text": "hello"},
        ts=1700000000.0,
    )
    a = make_event(**kwargs)
    b = make_event(**kwargs)
    assert a.id == b.id
    # Different seq → different id.
    c = make_event(**{**kwargs, "seq": 4})
    assert c.id != a.id


def test_event_id_ignores_metadata():
    """Metadata fields (latency, model) must not change the id."""
    base = dict(
        session_id="sess01", seq=1, type=EventType.ASSISTANT_MESSAGE,
        payload={"text": "ok"},
        ts=1700000000.0,
    )
    a = make_event(**base, latency_ms=100)
    b = make_event(**base, latency_ms=999, model="claude-x")
    assert a.id == b.id


def test_payload_validation_enforces_required_keys():
    with pytest.raises(PayloadError):
        make_event(session_id="s", seq=0, type=EventType.USER_PROMPT, payload={})

    # Missing tool_name:
    with pytest.raises(PayloadError):
        make_event(session_id="s", seq=0, type=EventType.PRE_TOOL_USE,
                   payload={"input": {}})


def test_roundtrip_json():
    ev = make_event(
        session_id="s", seq=7, type=EventType.MEMORY_WRITE,
        payload={"path": "/m.md", "kind": "project", "bytes": 42,
                 "content_hash": "abc"},
    )
    j = ev.to_json()
    parsed = Event.from_json(j)
    assert parsed == ev


def test_total_tokens_includes_cache():
    ev = make_event(
        session_id="s", seq=0, type=EventType.ASSISTANT_MESSAGE,
        payload={"text": "x"},
        tokens_in=100, tokens_out=50, cached_tokens=200, cache_creation_tokens=30,
    )
    assert ev.total_tokens == 100 + 50 + 200 + 30
