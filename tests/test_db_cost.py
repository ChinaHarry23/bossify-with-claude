"""Tests for the DB's cost-map helpers used by the boss dashboard."""
from __future__ import annotations

import pytest

from token_roi.events import EventType, make_event
from token_roi.pricing import OPUS_4_X, SONNET_4_X, calculate_usd_cost


def _emit_assistant(store, db, sid, *, model, tokens_in, tokens_out,
                    cache_read=0, cache_creation=0):
    seq = store.next_seq(sid)
    ev = make_event(
        session_id=sid, seq=seq,
        type=EventType.ASSISTANT_MESSAGE,
        payload={"text": "ok"},
        tokens_in=tokens_in, tokens_out=tokens_out,
        cached_tokens=cache_read, cache_creation_tokens=cache_creation,
        model=model,
    )
    store.append(ev)
    db.upsert_event(ev)
    return ev


def test_session_cost_map_is_priced_per_model(store, db):
    """A single session with mixed Opus + Sonnet turns must cost exactly
    ``opus_price + sonnet_price``, not a blended-average approximation."""
    sid = store.start_session()
    for ev in store.iter_session(sid):
        db.upsert_event(ev)

    _emit_assistant(store, db, sid,
                    model="claude-opus-4-7",
                    tokens_in=1_000_000, tokens_out=0)
    _emit_assistant(store, db, sid,
                    model="claude-sonnet-4-6",
                    tokens_in=1_000_000, tokens_out=0)

    cost = db.session_cost_map()[sid]
    expected = (
        calculate_usd_cost(1_000_000, 0, pricing=OPUS_4_X)
        + calculate_usd_cost(1_000_000, 0, pricing=SONNET_4_X)
    )
    assert cost == expected
    # Under the corrected Anthropic pricing (Opus 4.x dropped to $5/M
    # input, Sonnet stays $3/M input), 1M Opus + 1M Sonnet input tokens
    # bills at exactly $8 — strictly more than a Sonnet-only blended
    # estimate ($6) and strictly less than the OLD wrong Opus rate
    # ($15 + $3 = $18).
    assert cost > 6.0
    assert cost == pytest.approx(8.0)


def test_kpis_exposes_total_cost_usd(store, db):
    sid = store.start_session()
    for ev in store.iter_session(sid):
        db.upsert_event(ev)
    _emit_assistant(store, db, sid,
                    model="claude-sonnet-4-6",
                    tokens_in=500_000, tokens_out=100_000)

    k = db.kpis()
    assert k["sessions"] == 1
    assert k["tokens_in"] == 500_000
    assert k["tokens_out"] == 100_000
    expected = calculate_usd_cost(500_000, 100_000, pricing=SONNET_4_X)
    assert k["total_cost_usd"] == expected


def test_total_cost_sums_across_sessions(store, db):
    sid_a = store.start_session()
    for ev in store.iter_session(sid_a):
        db.upsert_event(ev)
    _emit_assistant(store, db, sid_a,
                    model="claude-opus-4-7",
                    tokens_in=100_000, tokens_out=0)

    sid_b = store.start_session()
    for ev in store.iter_session(sid_b):
        db.upsert_event(ev)
    _emit_assistant(store, db, sid_b,
                    model="claude-haiku-4-5",
                    tokens_in=100_000, tokens_out=0)

    total = db.total_cost()
    cost_map = db.session_cost_map()
    assert total == cost_map[sid_a] + cost_map[sid_b]
    # Opus must contribute more than Haiku for the same token count.
    assert cost_map[sid_a] > cost_map[sid_b]
