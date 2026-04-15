"""ROI classifier.

Takes an Attribution and assigns one of:

    HIGH_VALUE      — durable, reused, or drove positive outcome per token
    TRANSIENT_VALUE — produced output but didn't durably shift anything
    LOW_VALUE       — produced little for what it cost
    WASTED          — cost without durable or transient value; negative outcome

Scoring formula (documented in references/roi-model.md):

    v_durable  = log1p(durable_bytes) / log1p(2000)     # bytes written to memory
    v_reuse    = reuse_score                            # log-scaled cross-session hits
    v_outcome  = max(0, outcome_score)                  # only positive outcomes count as value
    v_negative = -min(0, outcome_score)                 # penalty term

    cost_unit  = cost_tokens / 1000  (so 1 = 1K tokens)

    numerator   = 0.4*v_durable + 0.35*v_reuse + 0.25*v_outcome
    denominator = max(0.25, cost_unit) + v_negative
    score       = numerator / denominator

Thresholds:
    HIGH      score >= 0.6  AND (durable OR reuse > 1 OR outcome > 0.5)
    TRANSIENT 0.25 <= score < 0.6
    LOW       0.05 <= score < 0.25
    WASTED    score < 0.05 OR (outcome_score < -0.5 AND reuse_score == 0)

Every score carries the full derivation so the CLI/dashboard can print the
"why" behind a classification. No magic numbers hide — they're in
OUTCOME_WEIGHTS and WEIGHTS below.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from enum import Enum

from .attribution import Attribution
from .db import AnalyticsDB


class ROIClass(str, Enum):
    HIGH_VALUE = "HIGH_VALUE"
    TRANSIENT_VALUE = "TRANSIENT_VALUE"
    LOW_VALUE = "LOW_VALUE"
    WASTED = "WASTED"


# Weights for the numerator. Changing these is a deliberate policy choice.
# `llm` is drawn from LLM_JUDGE_WEIGHT below and rebalances the others down
# when a judgment is present.
WEIGHTS = {
    "durable": 0.4,
    "reuse":   0.35,
    "outcome": 0.25,
}

# Scale constants (documented in roi-model.md; adjustable via config).
DURABLE_LOG_CEILING = 2000     # 2 KB of memory writes saturates the durable term
REUSE_SATURATION = 30          # 30 cross-session hits saturates reuse impact

# One "cost unit" = an average agentic turn's effective token cost.
# Calibrated against Claude Code data where a turn typically does 10-90 tool
# calls and runs 30K-500K effective tokens (cache reads discounted). With
# this scale, a max-value turn at the unit cost scores ~1.0, at 10x the unit
# cost scores ~0.1 (LOW_VALUE), and at 100x the unit cost scores ~0.01 (WASTED).
# A 3K-token trivial prompt falls under COST_FLOOR so it's fairly graded too.
COST_UNIT_TOKENS = 150_000
COST_FLOOR = 0.25

# --- retrospective proxy weights ---
# These activate when explicit signals are absent. Proxies are weaker than
# real signals but strong enough to produce a meaningful distribution on
# historical data (which otherwise scores uniformly near zero).
#
# Balance: a turn that wrote 100KB of code with 95% tool success should
# land at least in LOW_VALUE / TRANSIENT even without a memory write,
# because the code IS durable output — it just wasn't captured as memory.
# A live session with a real MEMORY_WRITE still beats an equivalent proxy
# because v_durable = max(memory, file_proxy) picks the stronger signal.
FILE_WRITE_DURABLE_WEIGHT = 0.8    # file writes count close-to-full-strength
FILE_WRITE_LOG_CEILING    = 20000  # 20 KB of file writes saturates the proxy

TOOL_SUCCESS_POS_THRESHOLD = 0.85  # success rate at/above this is a positive outcome proxy
TOOL_SUCCESS_NEG_THRESHOLD = 0.60  # success rate below this is a negative outcome proxy
TOOL_SUCCESS_MIN_CALLS     = 3     # need at least this many tool calls for the proxy
TOOL_SUCCESS_POS_WEIGHT    = 0.5   # equivalent to ~one `commit_created` outcome
TOOL_SUCCESS_NEG_WEIGHT    = -0.3

# --- LLM-judge weight ---
# When a local LLM has evaluated a prompt, its aggregate score is the
# strongest non-reuse signal available — it actually read the content.
# We give it its own slot in the numerator rather than folding into
# v_durable, because it also captures quality dimensions the byte-count
# proxies can't (is the code correct? was the response focused?).
LLM_JUDGE_WEIGHT = 0.30     # 0.30 out of a total weight budget of 1.0


@dataclass
class ROIScore:
    scope_kind: str
    scope_id: str
    cls: ROIClass
    score: float
    derivation: dict = field(default_factory=dict)
    computed_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "scope_kind": self.scope_kind,
            "scope_id": self.scope_id,
            "class": self.cls.value,
            "score": self.score,
            "derivation": self.derivation,
            "computed_at": self.computed_at,
        }


class ROIClassifier:
    """Stateless scorer. Keep one per-process for symmetry with other components."""

    def __init__(self, db: AnalyticsDB):
        self.db = db

    # ---- per-scope scoring ----

    def score_prompt(self, attribution: Attribution) -> ROIScore:
        # --- primary terms ---
        v_durable_mem = _log_saturate(attribution.durable_bytes, DURABLE_LOG_CEILING)
        v_reuse = min(attribution.reuse_score, _log_saturate(REUSE_SATURATION, REUSE_SATURATION))
        v_outcome_real = max(0.0, attribution.outcome_score)
        v_negative = -min(0.0, attribution.outcome_score)

        # --- optional LLM judgment ---
        # If the local LLM has judged this prompt, pull in its aggregate
        # score. This is the single strongest content-aware signal we have,
        # so it gets its own term in the numerator — it rebalances the
        # others downward proportionally so total numerator weight stays 1.0.
        llm_row = self.db.get_llm_judgment(attribution.prompt_event_id)
        if llm_row is not None:
            v_llm = float(llm_row["aggregate"] or 0.0)
            llm_weight = LLM_JUDGE_WEIGHT
            scale = 1.0 - llm_weight
            weights_effective = {k: v * scale for k, v in WEIGHTS.items()}
            weights_effective["llm"] = llm_weight
            # Use the LLM's efficiency score as a soft negative-outcome signal
            # when it's especially low (< 0.3) — the judge thinks the turn
            # was bloated, so we widen the denominator a bit.
            llm_eff = float(llm_row["efficiency"] or 0.0)
            if llm_eff < 0.3:
                v_negative += (0.3 - llm_eff)
        else:
            v_llm = 0.0
            llm_weight = 0.0
            weights_effective = dict(WEIGHTS)
            weights_effective["llm"] = 0.0

        # --- retrospective proxies ---
        # File writes stand in for durable output when the memory layer was
        # not active. At half weight so a real memory write of equal size
        # still beats it.
        v_durable_files = FILE_WRITE_DURABLE_WEIGHT * _log_saturate(
            attribution.file_write_bytes, FILE_WRITE_LOG_CEILING
        )
        v_durable = max(v_durable_mem, v_durable_files)

        # Tool success rate as a soft outcome signal. Requires a minimum
        # number of tool calls so a single lucky Bash doesn't move the score.
        v_outcome_proxy = 0.0
        success_rate = attribution.tool_success_rate
        if (
            success_rate is not None
            and attribution.tool_calls >= TOOL_SUCCESS_MIN_CALLS
        ):
            if success_rate >= TOOL_SUCCESS_POS_THRESHOLD:
                v_outcome_proxy = TOOL_SUCCESS_POS_WEIGHT
            elif success_rate < TOOL_SUCCESS_NEG_THRESHOLD:
                v_outcome_proxy = TOOL_SUCCESS_NEG_WEIGHT

        v_outcome = v_outcome_real + max(0.0, v_outcome_proxy)
        v_negative += max(0.0, -v_outcome_proxy)

        cost_unit = max(COST_FLOOR, attribution.cost_tokens / COST_UNIT_TOKENS)
        numerator = (weights_effective["durable"] * v_durable
                     + weights_effective["reuse"] * v_reuse
                     + weights_effective["outcome"] * v_outcome
                     + weights_effective["llm"] * v_llm)
        denominator = cost_unit + v_negative
        score = numerator / denominator

        cls = _classify(
            score, attribution,
            v_llm=v_llm if llm_weight else None,
            llm_efficiency=(float(llm_row["efficiency"]) if llm_row is not None else None),
        )

        derivation = {
            "formula": "numerator / denominator",
            "numerator": {
                "weights": weights_effective,
                "v_durable": v_durable,
                "v_durable_components": {
                    "memory_bytes": v_durable_mem,
                    "file_write_proxy": v_durable_files,
                    "picked": "memory_bytes" if v_durable_mem >= v_durable_files else "file_write_proxy",
                },
                "v_reuse": v_reuse,
                "v_outcome": v_outcome,
                "v_outcome_components": {
                    "real": v_outcome_real,
                    "tool_success_proxy": v_outcome_proxy,
                },
                "v_llm": v_llm,
                "llm_judgment": {
                    "present": llm_row is not None,
                    "meaningful_value": llm_row["meaningful_value"] if llm_row is not None else None,
                    "output_durability": llm_row["output_durability"] if llm_row is not None else None,
                    "code_quality": llm_row["code_quality"] if llm_row is not None else None,
                    "efficiency": llm_row["efficiency"] if llm_row is not None else None,
                    "reasoning": llm_row["reasoning"] if llm_row is not None else None,
                    "model": llm_row["model"] if llm_row is not None else None,
                },
            },
            "denominator": {
                "cost_unit": cost_unit,
                "v_negative": v_negative,
            },
            "inputs": {
                "cost_tokens": attribution.cost_tokens,
                "durable_bytes": attribution.durable_bytes,
                "retrieval_count": attribution.retrieval_count,
                "reuse_score": attribution.reuse_score,
                "outcome_score": attribution.outcome_score,
                "file_write_bytes": attribution.file_write_bytes,
                "tool_calls": attribution.tool_calls,
                "tool_successes": attribution.tool_successes,
                "tool_success_rate": success_rate,
            },
            "contributions": {
                "cost_events": attribution.cost_event_ids,
                "memory_writes": attribution.memory_write_ids,
                "retrieval_hits": attribution.retrieval_hit_event_ids,
                "outcomes": attribution.outcome_event_ids,
                "file_writes": attribution.file_write_event_ids,
            },
        }

        return ROIScore(
            scope_kind="prompt",
            scope_id=attribution.prompt_event_id,
            cls=cls,
            score=score,
            derivation=derivation,
        )

    def score_session(self, session_id: str) -> ROIScore:
        import json as _json
        rows = self.db._conn.execute(
            """SELECT * FROM attributions WHERE session_id = ?""",
            (session_id,),
        ).fetchall()
        # Cost-weighted average of per-prompt LLM aggregate scores for this
        # session. Feeds through to `_classify` via the synthesized
        # attribution so an LLM-blessed session isn't auto-WASTED just
        # because its total cost is large.
        llm_rows = self.db._conn.execute(
            """SELECT j.aggregate, j.efficiency, a.cost_tokens
                 FROM llm_judgments j
                 JOIN attributions a ON a.prompt_event_id = j.prompt_event_id
                WHERE a.session_id = ?""",
            (session_id,),
        ).fetchall()
        if llm_rows:
            total_w = sum((r["cost_tokens"] or 1) for r in llm_rows) or 1
            session_llm = sum((r["aggregate"] or 0) * (r["cost_tokens"] or 1) for r in llm_rows) / total_w
            session_llm_eff = sum((r["efficiency"] or 0) * (r["cost_tokens"] or 1) for r in llm_rows) / total_w
        else:
            session_llm = None
            session_llm_eff = None
        if not rows:
            empty = ROIScore(
                scope_kind="session", scope_id=session_id,
                cls=ROIClass.LOW_VALUE, score=0.0,
                derivation={"reason": "no attributions for session"},
            )
            self._persist(empty)
            return empty

        cost_tokens = sum(r["cost_tokens"] for r in rows)
        durable_bytes = sum(r["durable_bytes"] for r in rows)
        retrieval_count = sum(r["retrieval_count"] for r in rows)
        outcome_score = sum(r["outcome_score"] for r in rows)

        # Reuse at the session scope is straight sum of per-prompt reuse.
        reuse_score = sum(r["reuse_score"] for r in rows)

        # Sum the retrospective proxies too. At the session level they compose
        # the same way — total file writes, total tool calls, total successes.
        file_write_bytes = sum(r["file_write_bytes"] or 0 for r in rows)
        tool_calls = sum(r["tool_calls"] or 0 for r in rows)
        tool_successes = sum(r["tool_successes"] or 0 for r in rows)

        # Union all contribution ids so the session-level derivation still
        # names exact events, preserving the audit invariant.
        cost_ids: list[str] = []
        mw_ids: list[str] = []
        hit_ids: list[str] = []
        outcome_ids: list[str] = []
        fw_ids: list[str] = []
        for r in rows:
            cost_ids.extend(_json.loads(r["cost_event_ids_json"] or "[]"))
            mw_ids.extend(_json.loads(r["memory_write_ids_json"] or "[]"))
            hit_ids.extend(_json.loads(r["retrieval_hit_ids_json"] or "[]"))
            outcome_ids.extend(_json.loads(r["outcome_event_ids_json"] or "[]"))
            fw_ids.extend(_json.loads(r["file_write_event_ids_json"] or "[]"))

        synthesized = Attribution(
            prompt_event_id=f"session::{session_id}",
            session_id=session_id,
            cost_tokens=cost_tokens,
            durable_bytes=durable_bytes,
            retrieval_count=retrieval_count,
            outcome_score=outcome_score,
            reuse_score=reuse_score,
            file_write_bytes=file_write_bytes,
            tool_calls=tool_calls,
            tool_successes=tool_successes,
            cost_event_ids=cost_ids,
            memory_write_ids=mw_ids,
            retrieval_hit_event_ids=hit_ids,
            outcome_event_ids=outcome_ids,
            file_write_event_ids=fw_ids,
        )
        prompt_score = self.score_prompt(synthesized)
        # If a cost-weighted LLM aggregate exists, let it drive the session
        # classification via the same override band used for prompts. The
        # underlying numeric score is unchanged — this only affects the class.
        if session_llm is not None:
            cls = _classify(
                prompt_score.score, synthesized,
                v_llm=session_llm, llm_efficiency=session_llm_eff,
            )
        else:
            cls = prompt_score.cls

        session_score = ROIScore(
            scope_kind="session",
            scope_id=session_id,
            cls=cls,
            score=prompt_score.score,
            derivation={
                **prompt_score.derivation,
                "aggregated_prompts": len(rows),
                "contributing_prompt_ids": [r["prompt_event_id"] for r in rows],
                "session_llm_aggregate":  session_llm,
                "session_llm_efficiency": session_llm_eff,
            },
        )
        self._persist(session_score)
        return session_score

    def score_memory_write(self, memory_write_id: str) -> ROIScore:
        row = self.db._conn.execute(
            """SELECT * FROM memory_writes WHERE source_event_id = ?""",
            (memory_write_id,),
        ).fetchone()
        if row is None:
            raise KeyError(memory_write_id)
        hits = int(row["retrieval_hits"] or 0)
        # A memory write's score is hits-dominated with a floor so new writes
        # aren't immediately labeled WASTED.
        score = math.log1p(hits) / math.log1p(REUSE_SATURATION)
        if hits >= 5:
            cls = ROIClass.HIGH_VALUE
        elif hits >= 1:
            cls = ROIClass.TRANSIENT_VALUE
        elif row["ts"] > (time.time() - 3 * 24 * 3600):
            # less than 3 days old: don't punish yet
            cls = ROIClass.LOW_VALUE
        else:
            cls = ROIClass.WASTED

        result = ROIScore(
            scope_kind="memory_write",
            scope_id=memory_write_id,
            cls=cls,
            score=score,
            derivation={
                "retrieval_hits": hits,
                "last_retrieved": row["last_retrieved"],
                "age_days": (time.time() - row["ts"]) / 86400,
            },
        )
        self._persist(result)
        return result

    def score_all_prompts(self) -> int:
        import json as _json
        rows = self.db._conn.execute(
            """SELECT * FROM attributions"""
        ).fetchall()
        n = 0
        for r in rows:
            attribution = Attribution(
                prompt_event_id=r["prompt_event_id"],
                session_id=r["session_id"],
                cost_tokens=r["cost_tokens"],
                durable_bytes=r["durable_bytes"],
                retrieval_count=r["retrieval_count"],
                outcome_score=r["outcome_score"],
                reuse_score=r["reuse_score"],
                file_write_bytes=r["file_write_bytes"] or 0,
                tool_calls=r["tool_calls"] or 0,
                tool_successes=r["tool_successes"] or 0,
                cost_event_ids=_json.loads(r["cost_event_ids_json"] or "[]"),
                memory_write_ids=_json.loads(r["memory_write_ids_json"] or "[]"),
                retrieval_hit_event_ids=_json.loads(r["retrieval_hit_ids_json"] or "[]"),
                outcome_event_ids=_json.loads(r["outcome_event_ids_json"] or "[]"),
                file_write_event_ids=_json.loads(r["file_write_event_ids_json"] or "[]"),
            )
            self._persist(self.score_prompt(attribution))
            n += 1
        return n

    def score_all_memory_writes(self) -> int:
        rows = self.db._conn.execute(
            """SELECT source_event_id FROM memory_writes"""
        ).fetchall()
        for r in rows:
            self.score_memory_write(r["source_event_id"])
        return len(rows)

    def score_all_sessions(self) -> int:
        rows = self.db._conn.execute(
            """SELECT DISTINCT session_id FROM attributions"""
        ).fetchall()
        for r in rows:
            self.score_session(r["session_id"])
        return len(rows)

    # ---- helpers ----

    def _persist(self, roi: ROIScore) -> None:
        self.db.upsert_roi_score(
            scope_kind=roi.scope_kind,
            scope_id=roi.scope_id,
            roi_class=roi.cls.value,
            score=roi.score,
            derivation=roi.derivation,
            computed_at=roi.computed_at,
        )


def _log_saturate(x: float, ceiling: float) -> float:
    """log1p-based saturating map. f(0)=0, f(ceiling)=1, f(10*ceiling)~1.4."""
    if x <= 0:
        return 0.0
    return math.log1p(x) / math.log1p(ceiling)


def _classify(
    score: float,
    a: Attribution,
    *,
    v_llm: float | None = None,
    llm_efficiency: float | None = None,
) -> ROIClass:
    # ---- hard WASTED gate ----
    # Negative outcome with no offsetting value. File writes count against
    # this gate too; an LLM judgment above 0.3 also keeps the prompt out of
    # the hard gate (the judge saw real value in the content).
    if (
        a.outcome_score < -0.5
        and a.reuse_score == 0
        and a.durable_bytes == 0
        and a.file_write_bytes == 0
        and (v_llm is None or v_llm < 0.3)
    ):
        return ROIClass.WASTED

    # ---- LLM override band ----
    # When the local LLM has actually read the prompt + response + artifacts
    # and returned a strong verdict, we trust it over the pure cost-math.
    # The math can't distinguish "lots of good work that cost a lot" from
    # "lots of churn that cost a lot"; the LLM can.
    #
    # Override bands (only when v_llm is present, i.e. a judgment exists):
    #   v_llm >= 0.85 and efficiency >= 0.7  → HIGH_VALUE
    #   v_llm >= 0.85                        → TRANSIENT (efficiency drag)
    #   v_llm >= 0.60                        → TRANSIENT
    #   v_llm >= 0.35                        → LOW_VALUE
    #   v_llm <  0.35                        → WASTED
    #
    # The math-based threshold classification below remains the fallback
    # when no judgment is cached.
    if v_llm is not None:
        eff = llm_efficiency if llm_efficiency is not None else 0.5
        if v_llm >= 0.85 and eff >= 0.7:
            return ROIClass.HIGH_VALUE
        if v_llm >= 0.85:
            return ROIClass.TRANSIENT_VALUE
        if v_llm >= 0.60:
            return ROIClass.TRANSIENT_VALUE
        if v_llm >= 0.35:
            return ROIClass.LOW_VALUE
        return ROIClass.WASTED

    # ---- math-only classification (no LLM judgment present) ----
    if score < 0.05:
        return ROIClass.WASTED
    if score < 0.25:
        return ROIClass.LOW_VALUE
    proxy_positive = (
        a.file_write_bytes > 0
        or (a.tool_calls >= 3 and a.tool_successes / a.tool_calls >= 0.9)
    )
    if score >= 0.6 and (
        a.durable_bytes > 0
        or a.reuse_score > 1
        or a.outcome_score > 0.5
        or proxy_positive
    ):
        return ROIClass.HIGH_VALUE
    return ROIClass.TRANSIENT_VALUE
