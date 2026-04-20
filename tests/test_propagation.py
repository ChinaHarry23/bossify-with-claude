"""Cross-turn durable propagation tests.

A review/planning prompt that produces no artefact of its own gets a
decayed share of the next few prompts' durable output in the same
session — so the ROI dashboard stops flagging fruitful reviews as
WASTED when the fix landed one turn later.

These tests pin:
  - The propagation rule (decay by distance, eligibility by
    meaningful_value + no-durable-of-its-own).
  - The ROI classifier's tier-bump when propagated_bytes is
    non-trivial.
  - The end-to-end review → fix → classifier flow on a real event
    stream built through the store + attribution + classifier stack.
"""
from __future__ import annotations

import time

import pytest

from token_roi.attribution import AttributionGraph
from token_roi.events import EventType, make_event
from token_roi.roi import ROIClass, ROIClassifier


def _emit(store, db, sid, type_, **payload):
    seq = store.next_seq(sid)
    ev = make_event(session_id=sid, seq=seq, type=type_, **payload)
    store.append(ev)
    db.upsert_event(ev)
    return ev


def _add_judgment(db, prompt_id, *, meaningful, durability=0.5,
                  efficiency=0.5, aggregate=None):
    """Insert a cached LLM judgment directly. Lets us drive the
    propagation logic without running an actual LLM."""
    if aggregate is None:
        aggregate = (meaningful * durability * efficiency) ** (1.0 / 3.0)
    db._conn.execute(
        """INSERT OR REPLACE INTO llm_judgments
            (prompt_event_id, meaningful_value, code_quality,
             output_durability, efficiency, aggregate,
             reasoning, wasteful_patterns_json, model, judged_at)
           VALUES (?, ?, NULL, ?, ?, ?, 'r', '[]', 'test', ?)""",
        (prompt_id, meaningful, durability, efficiency, aggregate, time.time()),
    )


def test_review_then_fix_propagates_credit_backward(store, db):
    """Prompt 1 = review (0 durable, meaningful=0.6). Prompt 2 = fix
    (5KB file write, durable). After attribute_session, prompt 1 should
    carry ~50% × 5KB = 2500 propagated bytes."""
    sid = store.start_session()
    for ev in store.iter_session(sid):
        db.upsert_event(ev)

    review = _emit(store, db, sid, EventType.USER_PROMPT,
                   payload={"text": "review my code"})
    _emit(store, db, sid, EventType.ASSISTANT_MESSAGE,
          payload={"text": "here are the findings..."},
          parent_ids=(review.id,), tokens_in=500, tokens_out=1000)

    fix = _emit(store, db, sid, EventType.USER_PROMPT,
                payload={"text": "go ahead and fix all"})
    _emit(store, db, sid, EventType.ASSISTANT_MESSAGE,
          payload={"text": "done"}, parent_ids=(fix.id,),
          tokens_in=200, tokens_out=800)
    _emit(store, db, sid, EventType.FILE_WRITE,
          payload={"path": "fix.py", "content_hash": "h", "bytes": 5000},
          parent_ids=(fix.id,))

    # Seed LLM judgments. The review has meaningful=0.6 (above the 0.5
    # propagation floor); the fix has its own durable output.
    AttributionGraph(db)  # warm-up, not strictly required
    # Attribution runs, but propagation needs the LLM judgment cached
    # BEFORE it walks — so insert the judgment on a *pre-populated*
    # attribution row first. Simplest: run attribution once to populate
    # rows, add judgments, re-run attribution to propagate.
    graph = AttributionGraph(db)
    graph.attribute_session(sid)
    _add_judgment(db, review.id, meaningful=0.6)
    graph.attribute_session(sid)

    row = db._conn.execute(
        "SELECT propagated_bytes, propagated_from_json FROM attributions "
        "WHERE prompt_event_id = ?", (review.id,),
    ).fetchone()
    assert row["propagated_bytes"] == 2500
    import json
    pf = json.loads(row["propagated_from_json"])
    assert len(pf) == 1
    assert pf[0]["from_prompt"] == fix.id
    assert pf[0]["distance"] == 1
    assert pf[0]["bytes"] == 2500


def test_no_propagation_to_prompt_without_meaningful_judgment(store, db):
    """A review prompt with meaningful < 0.5 gets no propagation — the
    LLM saw no real content, so there's nothing to credit."""
    sid = store.start_session()
    for ev in store.iter_session(sid):
        db.upsert_event(ev)

    empty = _emit(store, db, sid, EventType.USER_PROMPT,
                  payload={"text": "?"})
    _emit(store, db, sid, EventType.ASSISTANT_MESSAGE,
          payload={"text": ""}, parent_ids=(empty.id,),
          tokens_in=10, tokens_out=2)
    fix = _emit(store, db, sid, EventType.USER_PROMPT,
                payload={"text": "make a change"})
    _emit(store, db, sid, EventType.ASSISTANT_MESSAGE,
          payload={"text": "ok"}, parent_ids=(fix.id,),
          tokens_in=100, tokens_out=500)
    _emit(store, db, sid, EventType.FILE_WRITE,
          payload={"path": "x.py", "content_hash": "h", "bytes": 5000},
          parent_ids=(fix.id,))

    graph = AttributionGraph(db)
    graph.attribute_session(sid)
    _add_judgment(db, empty.id, meaningful=0.2)   # below the 0.5 floor
    graph.attribute_session(sid)

    row = db._conn.execute(
        "SELECT propagated_bytes FROM attributions WHERE prompt_event_id = ?",
        (empty.id,),
    ).fetchone()
    assert row["propagated_bytes"] == 0


def test_no_propagation_to_prompt_with_own_durable_output(store, db):
    """If the parent already produced durable output, we don't credit
    it again — avoids double-counting."""
    sid = store.start_session()
    for ev in store.iter_session(sid):
        db.upsert_event(ev)

    reviewed_and_fixed = _emit(store, db, sid, EventType.USER_PROMPT,
                               payload={"text": "review+fix"})
    _emit(store, db, sid, EventType.ASSISTANT_MESSAGE,
          payload={"text": "done"}, parent_ids=(reviewed_and_fixed.id,),
          tokens_in=100, tokens_out=500)
    _emit(store, db, sid, EventType.FILE_WRITE,
          payload={"path": "a.py", "content_hash": "h", "bytes": 2000},
          parent_ids=(reviewed_and_fixed.id,))
    fix = _emit(store, db, sid, EventType.USER_PROMPT,
                payload={"text": "fix more"})
    _emit(store, db, sid, EventType.ASSISTANT_MESSAGE,
          payload={"text": "done"}, parent_ids=(fix.id,),
          tokens_in=100, tokens_out=500)
    _emit(store, db, sid, EventType.FILE_WRITE,
          payload={"path": "b.py", "content_hash": "h", "bytes": 5000},
          parent_ids=(fix.id,))

    graph = AttributionGraph(db)
    graph.attribute_session(sid)
    _add_judgment(db, reviewed_and_fixed.id, meaningful=0.8)
    graph.attribute_session(sid)

    row = db._conn.execute(
        "SELECT propagated_bytes, file_write_bytes FROM attributions "
        "WHERE prompt_event_id = ?", (reviewed_and_fixed.id,),
    ).fetchone()
    # Parent already wrote 2KB of its own — no propagation needed.
    assert row["propagated_bytes"] == 0
    assert row["file_write_bytes"] == 2000


def test_propagation_decays_by_distance(store, db):
    """Fix at position N propagates 50% to N-1, 25% to N-2, 10% to N-3."""
    sid = store.start_session()
    for ev in store.iter_session(sid):
        db.upsert_event(ev)

    p1 = _emit(store, db, sid, EventType.USER_PROMPT, payload={"text": "plan"})
    _emit(store, db, sid, EventType.ASSISTANT_MESSAGE,
          payload={"text": "ok"}, parent_ids=(p1.id,),
          tokens_in=50, tokens_out=50)
    p2 = _emit(store, db, sid, EventType.USER_PROMPT, payload={"text": "design"})
    _emit(store, db, sid, EventType.ASSISTANT_MESSAGE,
          payload={"text": "ok"}, parent_ids=(p2.id,),
          tokens_in=50, tokens_out=50)
    p3 = _emit(store, db, sid, EventType.USER_PROMPT, payload={"text": "review"})
    _emit(store, db, sid, EventType.ASSISTANT_MESSAGE,
          payload={"text": "ok"}, parent_ids=(p3.id,),
          tokens_in=50, tokens_out=50)
    p4 = _emit(store, db, sid, EventType.USER_PROMPT, payload={"text": "fix"})
    _emit(store, db, sid, EventType.ASSISTANT_MESSAGE,
          payload={"text": "done"}, parent_ids=(p4.id,),
          tokens_in=100, tokens_out=500)
    _emit(store, db, sid, EventType.FILE_WRITE,
          payload={"path": "x.py", "content_hash": "h", "bytes": 10_000},
          parent_ids=(p4.id,))

    graph = AttributionGraph(db)
    graph.attribute_session(sid)
    for p in (p1, p2, p3):
        _add_judgment(db, p.id, meaningful=0.6)
    graph.attribute_session(sid)

    def prop(pid):
        r = db._conn.execute(
            "SELECT propagated_bytes FROM attributions WHERE prompt_event_id = ?",
            (pid,),
        ).fetchone()
        return r["propagated_bytes"]

    # p3 is 1 step from p4 → 50% × 10_000 = 5_000
    # p2 is 2 steps → 25% × 10_000 = 2_500
    # p1 is 3 steps → 10% × 10_000 = 1_000
    assert prop(p3.id) == 5000
    assert prop(p2.id) == 2500
    assert prop(p1.id) == 1000


def test_propagation_bumps_review_from_wasted_to_low_value():
    """End-to-end: a review with aggregate 0.20 (would be WASTED even
    with the meaningful-floor patch because meaningful < 0.5 in this
    case) plus propagation credit should land at LOW_VALUE, not WASTED,
    because a real fix landed the next turn."""
    from token_roi.attribution import Attribution
    from token_roi.roi import _classify, _bump_one_tier

    a = Attribution(
        prompt_event_id="p", session_id="s",
        cost_tokens=50_000, durable_bytes=0, retrieval_count=0,
        outcome_score=0.0, reuse_score=0.0,
        propagated_bytes=2500,
        propagated_from=[{"from_prompt": "q", "bytes": 2500, "distance": 1}],
    )
    base = _classify(score=0.05, a=a, v_llm=0.2,
                     llm_efficiency=0.3, llm_meaningful=0.3)
    assert base == ROIClass.WASTED
    # The classifier in score_prompt applies _bump_one_tier explicitly
    # when propagated_bytes >= threshold. Simulate that here.
    bumped = _bump_one_tier(base)
    assert bumped == ROIClass.LOW_VALUE


def test_bump_caps_at_transient_value():
    """Propagation alone can never promote a prompt to HIGH_VALUE —
    that tier is reserved for cross-session reuse."""
    from token_roi.roi import _bump_one_tier
    assert _bump_one_tier(ROIClass.WASTED) == ROIClass.LOW_VALUE
    assert _bump_one_tier(ROIClass.LOW_VALUE) == ROIClass.TRANSIENT_VALUE
    # Already at the cap — no further bump.
    assert _bump_one_tier(ROIClass.TRANSIENT_VALUE) == ROIClass.TRANSIENT_VALUE
    assert _bump_one_tier(ROIClass.HIGH_VALUE) == ROIClass.HIGH_VALUE


def test_ultrareview_scenario_end_to_end(store, db):
    """The original motivating case: 'ultrareview my code base' → 'fix
    all' within the same session. Without propagation the review was
    labelled WASTED (score 0.063). With propagation crediting half of
    the fix's 38KB downstream artefacts, the review bumps to
    TRANSIENT_VALUE."""
    sid = store.start_session()
    for ev in store.iter_session(sid):
        db.upsert_event(ev)

    review = _emit(store, db, sid, EventType.USER_PROMPT,
                   payload={"text": "ultrareview my code base"})
    _emit(store, db, sid, EventType.ASSISTANT_MESSAGE,
          payload={"text": "findings..."}, parent_ids=(review.id,),
          tokens_in=10_000, tokens_out=40_000)

    fix = _emit(store, db, sid, EventType.USER_PROMPT,
                payload={"text": "go ahead and fix all"})
    _emit(store, db, sid, EventType.ASSISTANT_MESSAGE,
          payload={"text": "all tests pass"}, parent_ids=(fix.id,),
          tokens_in=5_000, tokens_out=20_000)
    for i, size in enumerate([10_000, 12_000, 8_000, 8_000]):
        _emit(store, db, sid, EventType.FILE_WRITE,
              payload={"path": f"fix_{i}.py", "content_hash": f"h{i}",
                       "bytes": size},
              parent_ids=(fix.id,))

    graph = AttributionGraph(db)
    graph.attribute_session(sid)
    # Review got meaningful=0.5 (same as the real session's judgment).
    _add_judgment(db, review.id, meaningful=0.5,
                  durability=0.3, efficiency=0.25)
    graph.attribute_session(sid)

    classifier = ROIClassifier(db)
    classifier.score_all_prompts()

    roi = db.get_roi_score("prompt", review.id)
    # Without propagation this would have been LOW_VALUE (via the
    # meaningful-floor rescue) or WASTED (without it). With 50% of
    # 38KB = 19KB propagated credit, the bump promotes to
    # TRANSIENT_VALUE.
    assert roi["class"] == "TRANSIENT_VALUE", roi["class"]
