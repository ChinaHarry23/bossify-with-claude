"""End-to-end attribution + ROI tests.

Construct synthetic event streams that exercise each ROI class boundary
and verify the classifier lands on the right side.
"""
from __future__ import annotations

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
