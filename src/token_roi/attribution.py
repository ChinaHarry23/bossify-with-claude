"""Token attribution engine.

Attribution answers: "for this prompt event, how did its tokens convert into
durable / retrieved / outcome value?"

The core idea is a **causal walk through the event DAG**:

    USER_PROMPT e0
      └── ASSISTANT_MESSAGE e1  (parent: e0)     <- primary cost
          ├── PRE_TOOL_USE e2   (parent: e1)
          │   └── POST_TOOL_USE e3   (parent: e2)
          │       └── FILE_WRITE e4  (indirect)
          ├── MEMORY_WRITE e5   (parent: e1)     <- durable value candidate
          └── RETRIEVAL_QUERY e6 (parent: e1)

For each USER_PROMPT we compute:

    cost_tokens     = tokens charged to e1 + any subsequent assistant messages
                      in the same "turn" (until the next USER_PROMPT)
    durable_bytes   = sum of MEMORY_WRITE byte payloads rooted at this prompt
    retrieval_count = number of future RETRIEVAL_RESULTS that hit a memory
                      write produced by this prompt (cross-session-safe)
    outcome_score   = weighted OUTCOME events in the same turn (test pass, etc.)
    reuse_score     = retrieval_count scaled by downstream-use indicator

Each contribution is written to `attributions` with a derivation record so
the ROI classifier can explain itself.

Critical property: **attribution is idempotent**. Running it twice produces
the same numbers. Running it after new events arrive produces higher or equal
retrieval_count and reuse_score but never downgrades a prompt.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Iterable

from .db import AnalyticsDB
from .events import Event, EventType


# Outcome-kind -> weight. Positive weights reward durable value; negatives
# punish destructive outcomes. Tune via roi-model.md.
OUTCOME_WEIGHTS: dict[str, float] = {
    "tests_passed":    1.0,
    "tests_failed":   -0.7,
    "commit_created":  0.6,
    "pr_merged":       1.2,
    "revert":         -1.0,
    "build_ok":        0.3,
    "build_failed":   -0.5,
    "user_accepted":   0.8,
    "user_rejected":  -1.0,
}


@dataclass
class Attribution:
    prompt_event_id: str
    session_id: str
    cost_tokens: int
    durable_bytes: int
    retrieval_count: int
    outcome_score: float
    reuse_score: float

    # Retrospective proxies — used when the live signals (memory writes,
    # retrievals, explicit outcomes) are absent, which is the common case
    # for imported historical data. The ROI classifier folds these into
    # v_durable / v_outcome with configurable weights so they don't
    # dominate when the real signals *are* present.
    file_write_bytes: int = 0       # sum of FILE_WRITE.payload.bytes in the turn
    tool_calls: int = 0             # POST_TOOL_USE count in the turn
    tool_successes: int = 0         # subset where success is true

    # Audit trail — which events contributed to which term.
    cost_event_ids: list[str] = field(default_factory=list)
    memory_write_ids: list[str] = field(default_factory=list)
    retrieval_hit_event_ids: list[str] = field(default_factory=list)
    outcome_event_ids: list[str] = field(default_factory=list)
    file_write_event_ids: list[str] = field(default_factory=list)

    @property
    def tool_success_rate(self) -> float | None:
        """Fraction of tool calls that succeeded, or None if no tool calls."""
        if self.tool_calls == 0:
            return None
        return self.tool_successes / self.tool_calls

    def derivation(self) -> dict:
        """Self-describing audit record persisted in roi_scores.derivation_json."""
        return {
            "prompt_event_id": self.prompt_event_id,
            "cost_tokens": self.cost_tokens,
            "durable_bytes": self.durable_bytes,
            "retrieval_count": self.retrieval_count,
            "outcome_score": self.outcome_score,
            "reuse_score": self.reuse_score,
            "contributions": {
                "cost_events": self.cost_event_ids,
                "memory_writes": self.memory_write_ids,
                "retrieval_hits": self.retrieval_hit_event_ids,
                "outcomes": self.outcome_event_ids,
            },
        }


class AttributionGraph:
    """Build + walk the event DAG for attribution.

    Construction is O(events in DB); query is O(events in a single session).
    """

    def __init__(self, db: AnalyticsDB):
        self.db = db

    # ---- top-level entrypoints ----

    def attribute_session(self, session_id: str) -> list[Attribution]:
        events = list(self.db.iter_session_events(session_id))
        if not events:
            return []
        events.sort(key=lambda e: e.seq)
        prompt_boundaries = self._prompt_boundaries(events)
        out: list[Attribution] = []
        for prompt_ev, turn_events in prompt_boundaries:
            attribution = self._attribute_turn(prompt_ev, turn_events)
            self._persist(attribution)
            out.append(attribution)
        # Second pass: cross-session retrieval attribution.
        self._update_cross_session_reuse(session_id, out)
        return out

    def attribute_all(self, session_ids: Iterable[str]) -> int:
        n = 0
        for sid in session_ids:
            n += len(self.attribute_session(sid))
        return n

    # ---- per-turn attribution ----

    @staticmethod
    def _prompt_boundaries(events: list[Event]) -> list[tuple[Event, list[Event]]]:
        """Partition a session into (prompt, turn_events) pairs.

        A turn ends at the next USER_PROMPT (or EOF). Events before the first
        prompt (SESSION_START, etc.) are dropped from attribution — they
        cannot be charged to any user action.
        """
        result: list[tuple[Event, list[Event]]] = []
        current_prompt: Event | None = None
        current_bucket: list[Event] = []
        for ev in events:
            if ev.type is EventType.USER_PROMPT:
                if current_prompt is not None:
                    result.append((current_prompt, current_bucket))
                current_prompt = ev
                current_bucket = [ev]
            elif current_prompt is not None:
                current_bucket.append(ev)
        if current_prompt is not None:
            result.append((current_prompt, current_bucket))
        return result

    def _attribute_turn(self, prompt: Event, turn: list[Event]) -> Attribution:
        cost_tokens = 0
        cost_ids: list[str] = []
        durable_bytes = 0
        memory_write_ids: list[str] = []
        outcome_score = 0.0
        outcome_ids: list[str] = []
        file_write_bytes = 0
        file_write_ids: list[str] = []
        tool_calls = 0
        tool_successes = 0

        for ev in turn:
            if ev.type is EventType.ASSISTANT_MESSAGE:
                # Use effective cost (cache reads at 0.1x) not total flow,
                # or cache-dominated sessions will drown the signal.
                cost_tokens += ev.effective_cost_tokens
                cost_ids.append(ev.id)
            elif ev.type is EventType.MEMORY_WRITE:
                durable_bytes += int(ev.payload.get("bytes") or 0)
                memory_write_ids.append(ev.id)
            elif ev.type is EventType.FILE_WRITE:
                # File writes outside data/memory are still durable output —
                # the agent produced real code. Count them as a proxy for
                # durable value when explicit memory writes are absent.
                file_write_bytes += int(ev.payload.get("bytes") or 0)
                file_write_ids.append(ev.id)
            elif ev.type is EventType.POST_TOOL_USE:
                tool_calls += 1
                if ev.payload.get("success"):
                    tool_successes += 1
            elif ev.type is EventType.OUTCOME:
                kind = ev.payload.get("kind") or ""
                weight = OUTCOME_WEIGHTS.get(kind, 0.0)
                outcome_score += weight
                if weight:
                    outcome_ids.append(ev.id)

        # Same-turn retrieval-hit counting: which of this turn's retrievals
        # hit memory written by THIS prompt? Those are self-reinforcing loops,
        # not durable-value signals; we count only cross-turn/session reuse.
        # (Cross-session reuse is handled in _update_cross_session_reuse.)

        return Attribution(
            prompt_event_id=prompt.id,
            session_id=prompt.session_id,
            cost_tokens=cost_tokens,
            durable_bytes=durable_bytes,
            retrieval_count=0,
            outcome_score=outcome_score,
            reuse_score=0.0,
            file_write_bytes=file_write_bytes,
            tool_calls=tool_calls,
            tool_successes=tool_successes,
            cost_event_ids=cost_ids,
            memory_write_ids=memory_write_ids,
            outcome_event_ids=outcome_ids,
            file_write_event_ids=file_write_ids,
        )

    # ---- cross-session reuse ----

    def _update_cross_session_reuse(self, session_id: str, attributions: list[Attribution]) -> None:
        """Walk the retrievals table for hits against this session's memory writes.

        For each memory write produced by a prompt in `attributions`, count
        retrievals (in any session, at any later time) that included that
        memory_write_id in their hits. The retrieval_count + last_retrieved
        columns on memory_writes are kept in sync as a side-effect.
        """
        if not attributions:
            return
        # Map memory_write_id -> prompt_event_id
        mw_to_prompt: dict[str, str] = {}
        for a in attributions:
            for mw in a.memory_write_ids:
                mw_to_prompt[mw] = a.prompt_event_id

        if not mw_to_prompt:
            return

        # Query every retrieval in the DB that references any of our memory writes.
        placeholders = ",".join("?" for _ in mw_to_prompt)
        rows = self.db._conn.execute(
            f"""
            SELECT source_event_id, session_id, ts, hit_ids_json
            FROM retrievals
            WHERE EXISTS (
                SELECT 1 FROM json_each(retrievals.hit_ids_json) j
                WHERE j.value IN ({placeholders})
            )
            """,
            tuple(mw_to_prompt.keys()),
        ).fetchall()

        reuse_count: dict[str, int] = {pid: 0 for pid in {a.prompt_event_id for a in attributions}}
        hit_events: dict[str, list[str]] = {pid: [] for pid in reuse_count}

        for r in rows:
            hit_ids = json.loads(r["hit_ids_json"])
            for mw in hit_ids:
                pid = mw_to_prompt.get(mw)
                if pid is None:
                    continue
                reuse_count[pid] = reuse_count.get(pid, 0) + 1
                hit_events.setdefault(pid, []).append(r["source_event_id"])
                # Update memory_writes.retrieval_hits counter.
                self.db.increment_memory_hit(mw, r["ts"])
                # Mark the retrieval itself as "used" if it landed within
                # a useful distance of the prompt that authored this memory.
                # (Soft signal: we just flag every cross-session retrieval as used.)
                if r["session_id"] != session_id:
                    self.db.mark_retrieval_used(r["source_event_id"])

        # Persist updated retrieval_count + reuse_score on attributions.
        for a in attributions:
            a.retrieval_count = reuse_count.get(a.prompt_event_id, 0)
            # reuse_score: diminishing returns on repeat hits so a single
            # memory that gets spammed doesn't dominate.
            a.reuse_score = _diminishing(a.retrieval_count)
            a.retrieval_hit_event_ids = hit_events.get(a.prompt_event_id, [])
            self._persist(a)

    def _persist(self, a: Attribution) -> None:
        self.db.upsert_attribution(
            prompt_event_id=a.prompt_event_id,
            session_id=a.session_id,
            cost_tokens=a.cost_tokens,
            durable_bytes=a.durable_bytes,
            retrieval_count=a.retrieval_count,
            outcome_score=a.outcome_score,
            reuse_score=a.reuse_score,
            file_write_bytes=a.file_write_bytes,
            tool_calls=a.tool_calls,
            tool_successes=a.tool_successes,
            cost_event_ids=a.cost_event_ids,
            memory_write_ids=a.memory_write_ids,
            retrieval_hit_ids=a.retrieval_hit_event_ids,
            outcome_event_ids=a.outcome_event_ids,
            file_write_event_ids=a.file_write_event_ids,
        )


def _diminishing(n: int) -> float:
    """Concave mapping from hit count to reuse score.

    f(0) = 0.0, f(1) = 1.0, f(5) ~ 2.3, f(30) ~ 3.4.
    Prevents a single popular memory entry from dominating ROI accounting.
    """
    if n <= 0:
        return 0.0
    import math
    return math.log1p(n) / math.log1p(1)
