"""Tests for synthetic-prompt detection + downstream filtering."""
from __future__ import annotations

import pytest

from token_roi.attribution import AttributionGraph
from token_roi.events import EventType, is_synthetic_prompt, make_event
from token_roi.roi import ROIClass, ROIClassifier


def test_real_prompts_are_not_flagged():
    assert not is_synthetic_prompt("fix the failing test")
    assert not is_synthetic_prompt("I want to use opus instead of sonnet?")
    assert not is_synthetic_prompt("You are lesson writer D. Produce 4 React TSX files.")


def test_command_plumbing_is_flagged():
    # Claude Code's slash-command wrappers.
    assert is_synthetic_prompt("<local-command-caveat>Caveat: messages below…")
    assert is_synthetic_prompt("<local-command-stdout>Set model to claude-opus")
    assert is_synthetic_prompt("<local-command-stderr>oops")
    assert is_synthetic_prompt("<command-name>/model</command-name>\n<command-message>…")


def test_task_notifications_are_flagged():
    assert is_synthetic_prompt("<task-notification>\n<task-id>abc</task-id>")


def test_post_compaction_continuation_is_flagged():
    # What Claude Code injects after /compact — there's no user intent here.
    text = (
        "This session is being continued from a previous conversation "
        "that ran out of context. The summary below covers the earlier portion…"
    )
    assert is_synthetic_prompt(text)


def test_empty_and_whitespace_are_flagged():
    assert is_synthetic_prompt("")
    assert is_synthetic_prompt(None)
    assert is_synthetic_prompt("   \n\t  ")


def test_attribution_skips_synthetic_prompt_turns(store, db):
    """A synthetic USER_PROMPT must not carve its own attribution row.

    We seed: real prompt → assistant reply → synthetic prompt → more
    assistant work. The synthetic one should fold into the preceding
    real turn rather than starting a new one.
    """
    sid = store.start_session()
    for ev in store.iter_session(sid):
        db.upsert_event(ev)

    def emit(type_, **payload):
        seq = store.next_seq(sid)
        ev = make_event(session_id=sid, seq=seq, type=type_, **payload)
        store.append(ev)
        db.upsert_event(ev)
        return ev

    real = emit(EventType.USER_PROMPT, payload={"text": "do the thing"})
    emit(EventType.ASSISTANT_MESSAGE, payload={"text": "ok"},
         parent_ids=(real.id,), tokens_in=500, tokens_out=200)
    # This should NOT produce its own attribution row.
    synth = emit(EventType.USER_PROMPT,
                 payload={"text": "<local-command-stdout>Set model to X"})
    emit(EventType.ASSISTANT_MESSAGE, payload={"text": "noted"},
         parent_ids=(synth.id,), tokens_in=100, tokens_out=50)

    graph = AttributionGraph(db)
    atts = graph.attribute_session(sid)
    # Exactly one attribution, keyed on the real prompt.
    assert len(atts) == 1
    assert atts[0].prompt_event_id == real.id
    # Synthetic prompt's downstream assistant work rolls into the real turn.
    assert atts[0].cost_tokens > 0


def test_purge_synthetic_removes_old_rows(store, db):
    """Older DBs (imported before the filter landed) still carry synthetic
    attribution rows. ``purge_synthetic_prompts`` cleans them without
    touching raw events."""
    sid = store.start_session()
    for ev in store.iter_session(sid):
        db.upsert_event(ev)

    # Directly insert a synthetic USER_PROMPT event, simulating pre-filter data.
    synth = make_event(
        session_id=sid, seq=store.next_seq(sid),
        type=EventType.USER_PROMPT,
        payload={"text": "<task-notification>\n<task-id>abc</task-id>"},
    )
    store.append(synth)
    db.upsert_event(synth)
    # Fabricate an attribution row anchored on the synthetic id (old code path).
    db.upsert_attribution(
        prompt_event_id=synth.id, session_id=sid,
        cost_tokens=1000, durable_bytes=0, retrieval_count=0,
        outcome_score=0.0, reuse_score=0.0,
    )

    counts = db.purge_synthetic_prompts()
    assert counts["synthetic_found"] == 1
    assert counts["attributions"] == 1
    # Raw event is still in the store (audit invariant) — we only
    # touched derived rows.
    remaining = [e for e in store.iter_session(sid) if e.id == synth.id]
    assert len(remaining) == 1
