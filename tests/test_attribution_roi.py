"""End-to-end attribution + ROI tests.

Construct synthetic event streams that exercise each ROI class boundary
and verify the classifier lands on the right side.
"""
from __future__ import annotations

import pytest

from token_roi.attribution import AttributionGraph
from token_roi.db import AnalyticsDB
from token_roi.events import EventType, make_event
from token_roi.roi import ROIClass, ROIClassifier


def _emit(store, db, sid, type_, **payload):
    """Helper: append via store AND mirror to the DB so both are in sync."""
    seq = store._next_seq(sid)
    ev = make_event(session_id=sid, seq=seq, type=type_, **payload)
    store.append(ev)
    db.upsert_event(ev)
    return ev


def test_high_value_prompt(store, db):
    sid = store.start_session()
    # SESSION_START already in store. Mirror to DB.
    for ev in store.iter_session(sid):
        db.upsert_event(ev)

    prompt = _emit(store, db, sid, EventType.USER_PROMPT, payload={"text": "plan the auth rewrite"})
    _emit(store, db, sid, EventType.ASSISTANT_MESSAGE,
          payload={"text": "here is the plan..."},
          parent_ids=(prompt.id,),
          tokens_in=200, tokens_out=300)
    _emit(store, db, sid, EventType.MEMORY_WRITE,
          payload={"path": "m.md", "kind": "project", "bytes": 1200, "content_hash": "h"},
          parent_ids=(prompt.id,))
    _emit(store, db, sid, EventType.OUTCOME,
          payload={"kind": "commit_created", "detail": "..."},
          parent_ids=(prompt.id,))

    graph = AttributionGraph(db)
    attributions = graph.attribute_session(sid)
    assert len(attributions) == 1
    a = attributions[0]
    assert a.durable_bytes == 1200
    assert a.outcome_score == 0.6

    classifier = ROIClassifier(db)
    score = classifier.score_prompt(a)
    # Cheap prompt with durable + outcome should land in the upper tier.
    assert score.cls in {ROIClass.HIGH_VALUE, ROIClass.TRANSIENT_VALUE}
    assert score.score > 0.25


def test_wasted_prompt(store, db):
    sid = store.start_session()
    for ev in store.iter_session(sid):
        db.upsert_event(ev)

    prompt = _emit(store, db, sid, EventType.USER_PROMPT, payload={"text": "retry build"})
    _emit(store, db, sid, EventType.ASSISTANT_MESSAGE,
          payload={"text": "build failed again"},
          parent_ids=(prompt.id,),
          tokens_in=500, tokens_out=200)
    _emit(store, db, sid, EventType.OUTCOME,
          payload={"kind": "build_failed"},
          parent_ids=(prompt.id,))

    graph = AttributionGraph(db)
    attributions = graph.attribute_session(sid)
    a = attributions[0]
    assert a.durable_bytes == 0
    assert a.outcome_score == -0.5

    classifier = ROIClassifier(db)
    score = classifier.score_prompt(a)
    # Negative outcome, no durable, no reuse → WASTED or LOW_VALUE.
    assert score.cls in {ROIClass.WASTED, ROIClass.LOW_VALUE}


def test_retrospective_proxies_lift_historical_data(store, db):
    """A turn with file writes + high tool success should outscore a turn
    with neither, even when both lack memory writes / retrievals / outcomes.

    This exercises the path that lets imported Claude Code history produce
    a meaningful ROI distribution. Without the proxies, every historical
    prompt collapses to score=0 and class=WASTED.
    """
    sid = store.start_session()
    for ev in store.iter_session(sid):
        db.upsert_event(ev)

    def emit(type_, **payload):
        seq = store._next_seq(sid)
        ev = make_event(session_id=sid, seq=seq, type=type_, **payload)
        store.append(ev)
        db.upsert_event(ev)
        return ev

    # PROMPT A — produces 10 KB of file writes with 9/10 tool successes.
    pa = emit(EventType.USER_PROMPT, payload={"text": "write some code"})
    emit(EventType.ASSISTANT_MESSAGE,
         payload={"text": "done"},
         parent_ids=(pa.id,),
         tokens_in=1000, tokens_out=2000)
    for i in range(10):
        pre = emit(EventType.PRE_TOOL_USE,
                   payload={"tool_name": "Write", "input": {"file_path": f"f{i}.py"}})
        emit(EventType.POST_TOOL_USE,
             payload={"tool_name": "Write", "success": i != 3},  # 9/10 success
             parent_ids=(pre.id,))
        emit(EventType.FILE_WRITE,
             payload={"path": f"f{i}.py", "content_hash": "h",
                      "bytes": 1000},
             parent_ids=(pre.id,))

    # PROMPT B — same cost, zero file writes, zero tool calls.
    pb = emit(EventType.USER_PROMPT, payload={"text": "chat only"})
    emit(EventType.ASSISTANT_MESSAGE,
         payload={"text": "here is some prose"},
         parent_ids=(pb.id,),
         tokens_in=1000, tokens_out=2000)

    graph = AttributionGraph(db)
    atts = {a.prompt_event_id: a for a in graph.attribute_session(sid)}
    a_a = atts[pa.id]
    a_b = atts[pb.id]

    assert a_a.file_write_bytes == 10_000
    assert a_a.tool_calls == 10
    assert a_a.tool_successes == 9
    assert a_b.file_write_bytes == 0
    assert a_b.tool_calls == 0

    classifier = ROIClassifier(db)
    sa = classifier.score_prompt(a_a)
    sb = classifier.score_prompt(a_b)
    # A must be strictly better than B.
    assert sa.score > sb.score
    # And A should not be WASTED despite zero memory writes.
    assert sa.cls != ROIClass.WASTED
    # B still is WASTED (score ~0).
    assert sb.cls == ROIClass.WASTED


def test_meaningful_floor_rescues_ephemeral_real_work_from_wasted():
    """A review-style prompt (meaningful=0.5, durability=0.3, efficiency=0.25)
    has aggregate ≈ 0.335 — under the old classifier it would land in
    WASTED, because the cube-root aggregate dragged below 0.35. But
    the LLM still judged the *content* meaningful (>= 0.5), so the
    right class is LOW_VALUE, not WASTED.

    This is the 'code review that finds real bugs but produces zero
    files' case — real work, just not captured durably. Calling it
    WASTED mis-represents what happened.
    """
    from token_roi.attribution import Attribution
    from token_roi.roi import _classify

    a = Attribution(
        prompt_event_id="p",
        session_id="s",
        cost_tokens=436_000,
        durable_bytes=0,
        retrieval_count=0,
        outcome_score=0.0,
        reuse_score=0.0,
        file_write_bytes=0,
        tool_calls=13,
        tool_successes=13,
    )
    # Aggregate 0.335 = (0.5 * 0.3 * 0.25) ** (1/3).
    cls = _classify(
        score=0.063, a=a,
        v_llm=0.335, llm_efficiency=0.25, llm_meaningful=0.50,
    )
    assert cls.value == "LOW_VALUE", cls


def test_low_meaningful_prompt_is_still_wasted():
    """Regression guard: a prompt with meaningful < 0.5 AND aggregate
    < 0.35 must still land in WASTED. We're softening the WASTED band
    only for real-work-with-no-artifact, not for everything."""
    from token_roi.attribution import Attribution
    from token_roi.roi import _classify

    a = Attribution(
        prompt_event_id="p", session_id="s",
        cost_tokens=50_000, durable_bytes=0, retrieval_count=0,
        outcome_score=0.0, reuse_score=0.0,
    )
    # aggregate 0.15 from low meaning/durability/efficiency all round.
    cls = _classify(
        score=0.02, a=a,
        v_llm=0.15, llm_efficiency=0.2, llm_meaningful=0.3,
    )
    assert cls.value == "WASTED"


def test_diminishing_is_normalised_to_reuse_saturation():
    """``_diminishing`` must map REUSE_SATURATION hits to 1.0 so the ROI
    layer's ``min(reuse_score, 1.0)`` cap aligns with the attribution
    scale. A previous implementation divided by ``log1p(1)``, which
    caused a single reuse hit to already saturate the reuse term."""
    import math

    from token_roi.attribution import _diminishing
    from token_roi.roi import REUSE_SATURATION

    assert _diminishing(0) == 0.0
    # A single hit should NOT already be at the cap.
    assert 0.0 < _diminishing(1) < 0.5
    # Saturation point equals exactly 1.0.
    assert _diminishing(REUSE_SATURATION) == pytest.approx(1.0)
    # Monotonic, concave: per-hit marginal return strictly diminishes.
    f1, f2, f5, f30 = _diminishing(1), _diminishing(2), _diminishing(5), _diminishing(30)
    assert f1 < f2 < f5 < f30
    slope_1_2 = f2 - f1
    slope_5_30 = (f30 - f5) / (30 - 5)
    assert slope_1_2 > slope_5_30


def test_cross_session_reuse_upgrades_score(store, db):
    # Session A: writes memory.
    sid_a = store.start_session()
    for ev in store.iter_session(sid_a):
        db.upsert_event(ev)
    prompt_a = _emit(store, db, sid_a, EventType.USER_PROMPT,
                     payload={"text": "capture the design"})
    _emit(store, db, sid_a, EventType.ASSISTANT_MESSAGE,
          payload={"text": "captured"}, parent_ids=(prompt_a.id,),
          tokens_in=100, tokens_out=200)
    mw = _emit(store, db, sid_a, EventType.MEMORY_WRITE,
               payload={"path": "m.md", "kind": "project",
                        "bytes": 400, "content_hash": "h"},
               parent_ids=(prompt_a.id,))

    # Baseline attribution before reuse.
    graph = AttributionGraph(db)
    graph.attribute_session(sid_a)
    base = db._conn.execute(
        "SELECT retrieval_count, reuse_score FROM attributions WHERE prompt_event_id=?",
        (prompt_a.id,)
    ).fetchone()
    assert base["retrieval_count"] == 0

    # Session B: retrieval hits Session A's memory.
    sid_b = store.start_session()
    for ev in store.iter_session(sid_b):
        db.upsert_event(ev)
    prompt_b = _emit(store, db, sid_b, EventType.USER_PROMPT,
                     payload={"text": "remind me of the design"})
    q = _emit(store, db, sid_b, EventType.RETRIEVAL_QUERY,
              payload={"query": "design"}, parent_ids=(prompt_b.id,))
    _emit(store, db, sid_b, EventType.RETRIEVAL_RESULT,
          payload={"query": "design", "hits": [
              {"memory_write_id": mw.id, "doc_id": "memory::m.md",
               "kind": "memory", "score": 0.8, "title": "m"}
          ]},
          parent_ids=(q.id,))
    graph.attribute_session(sid_b)
    # Re-run session A attribution to pick up the new reuse signal.
    graph.attribute_session(sid_a)

    after = db._conn.execute(
        "SELECT retrieval_count, reuse_score FROM attributions WHERE prompt_event_id=?",
        (prompt_a.id,)
    ).fetchone()
    assert after["retrieval_count"] == 1
    assert after["reuse_score"] > 0
