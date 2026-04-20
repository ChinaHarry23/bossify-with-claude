"""Tests for the aggregate-formula fix + session/project naming plumbing."""
from __future__ import annotations

import time

import pytest

from token_roi.events import EventType, make_event


def test_aggregate_now_folds_efficiency():
    """Regression guard for the 'aggregate=1.0 at efficiency=0.3' bug.

    Under the fixed formula (cube-root of meaning × durability ×
    efficiency), a session that accomplished the task but burned
    absurd tokens cannot earn a perfect aggregate.
    """
    from token_roi.llm_judge import Judgment

    j = Judgment(
        prompt_event_id="x",
        meaningful_value=1.0,
        code_quality=0.8,
        output_durability=1.0,
        efficiency=0.3,
        reasoning="r",
        wasteful_patterns=[],
        model="test",
    )
    # (1.0 * 1.0 * 0.3) ** (1/3) ≈ 0.669
    assert 0.66 < j.aggregate < 0.68, j.aggregate


def test_aggregate_bounded_and_zero_when_any_component_zero():
    from token_roi.llm_judge import Judgment

    # Perfect meaning + durability, zero efficiency → aggregate goes to 0.
    j_zero_eff = Judgment(
        prompt_event_id="x", meaningful_value=1.0, code_quality=None,
        output_durability=1.0, efficiency=0.0,
        reasoning="r", wasteful_patterns=[], model="t",
    )
    assert j_zero_eff.aggregate == 0.0

    # Out-of-range inputs clamped before the geometric mean.
    j_clamped = Judgment(
        prompt_event_id="x", meaningful_value=1.5, code_quality=None,
        output_durability=-0.5, efficiency=0.9,
        reasoning="r", wasteful_patterns=[], model="t",
    )
    assert 0.0 <= j_clamped.aggregate <= 1.0


def test_recompute_llm_aggregates_updates_stored_rows(store, db):
    """Existing rows written under the old formula must be rewritten by
    ``recompute_llm_aggregates``. Without this step a user who upgrades
    keeps stale cached aggregates forever."""
    # llm_judgments.prompt_event_id is FK-constrained to events.id so we
    # need a real event on which to hang the judgment row.
    sid = store.start_session()
    for ev in store.iter_session(sid):
        db.upsert_event(ev)
    prompt = make_event(
        session_id=sid, seq=store.next_seq(sid),
        type=EventType.USER_PROMPT, payload={"text": "do it"},
    )
    store.append(prompt)
    db.upsert_event(prompt)

    # Write a judgment with an intentionally wrong aggregate, as the old
    # formula would have produced.
    db._conn.execute(
        """INSERT INTO llm_judgments
           (prompt_event_id, meaningful_value, code_quality, output_durability,
            efficiency, aggregate, reasoning, wasteful_patterns_json, model, judged_at)
           VALUES (?, 1.0, NULL, 1.0, 0.3, 1.0, 'r', '[]', 'test', ?)""",
        (prompt.id, time.time()),
    )
    row_before = db.get_llm_judgment(prompt.id)
    assert row_before["aggregate"] == 1.0

    n = db.recompute_llm_aggregates()
    assert n == 1

    row_after = db.get_llm_judgment(prompt.id)
    # Same row, but aggregate now reflects the efficiency drag.
    assert 0.66 < row_after["aggregate"] < 0.68


def test_sessions_needing_summary_includes_empty_name_placeholders(store, db):
    """Placeholder rows written by ``upsert_session_metadata`` have empty
    names; the naming pass must NOT skip them."""
    from token_roi.llm_judge import Judge, LocalLLM

    sid = store.start_session()
    for ev in store.iter_session(sid):
        db.upsert_event(ev)
    # Importer behaviour: tag the session with a project slug but no name yet.
    db.upsert_session_metadata(sid, project_slug="my-project", employee_id="alice")

    # Before the fix this query returned an empty list because a
    # placeholder row exists, tricking name-sessions into thinking we're
    # already done.
    judge = Judge(db, LocalLLM())  # no LLM calls here — just the query
    pending = judge.sessions_needing_summary()
    assert sid in pending


def test_upsert_session_summary_preserves_project_slug(db):
    """INSERT OR REPLACE on the naming columns used to wipe the metadata
    columns (REPLACE is a DELETE + INSERT). The fix splits the upsert
    into INSERT-when-new vs UPDATE-when-existing."""
    from token_roi.llm_judge import SessionSummary

    db.upsert_session_metadata("sid1", project_slug="algo", employee_id="alice")
    db.upsert_session_summary(SessionSummary(
        session_id="sid1",
        name="Algorithms learning site",
        summary="Scaffolded CLRS lesson hub.",
        model="glm-test",
    ))

    row = db._conn.execute(
        """SELECT name, summary, project_slug, employee_id
             FROM session_summaries WHERE session_id = 'sid1'"""
    ).fetchone()
    assert row["name"] == "Algorithms learning site"
    assert row["summary"] == "Scaffolded CLRS lesson hub."
    # Critical: project_slug / employee_id must survive the naming upsert.
    assert row["project_slug"] == "algo"
    assert row["employee_id"] == "alice"


def test_projects_with_stats_aggregates_by_slug(store, db):
    """Sessions sharing a project_slug roll up into one project row."""
    from token_roi.pricing import SONNET_4_X, calculate_usd_cost

    # Two sessions under the same project slug.
    sid_a = "s_a"
    sid_b = "s_b"
    db.upsert_session_metadata(sid_a, project_slug="my-app")
    db.upsert_session_metadata(sid_b, project_slug="my-app")
    # One session under a different slug (should land in its own project).
    db.upsert_session_metadata("s_other", project_slug="other-app")

    for sid, tokens_in in [(sid_a, 1_000_000), (sid_b, 500_000), ("s_other", 100_000)]:
        store.start_session(sid)
        for ev in store.iter_session(sid):
            db.upsert_event(ev)
        ev = make_event(
            session_id=sid, seq=store.next_seq(sid),
            type=EventType.ASSISTANT_MESSAGE, payload={"text": "ok"},
            tokens_in=tokens_in, tokens_out=0,
            model="claude-sonnet-4-6",
        )
        store.append(ev)
        db.upsert_event(ev)

    projects = db.projects_with_stats()
    by_slug = {p["slug"]: p for p in projects}
    assert "my-app" in by_slug and "other-app" in by_slug
    assert by_slug["my-app"]["session_count"] == 2
    assert by_slug["other-app"]["session_count"] == 1

    # Cost adds up across the two sessions in my-app.
    expected = calculate_usd_cost(1_500_000, 0, pricing=SONNET_4_X)
    assert by_slug["my-app"]["cost_usd"] == pytest.approx(expected)
    assert by_slug["my-app"]["cost_usd"] > by_slug["other-app"]["cost_usd"]
