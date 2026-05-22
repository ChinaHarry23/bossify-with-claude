"""Memory compression engine.

Takes the raw event log and produces / refreshes the compressed memory layer
(`MEMORY.md` + `topics/*.md`).

Design:
    - Clustering is **topical**, not temporal. Two sessions a month apart can
      contribute to the same topic if they concern the same work.
    - Every produced topic file carries `source_events` — the full list of
      raw event ids that contributed — so attribution can trace.
    - Compression is **deterministic** given the same event stream. We do not
      call an LLM from here by default (though there is an optional hook for
      that — see `summarize_fn` parameter).
    - Budget: the MEMORY.md index stays under 200 lines (spec), and a single
      compression pass aims for ~6K total tokens across all topic bodies.

Clustering strategy:
    1. Collect all ASSISTANT_MESSAGE events + their USER_PROMPT parents.
    2. Group by embedding-proximate centroid (if sentence-transformers is
       available) or by lexical overlap (fallback).
    3. For each cluster, emit / update a topic file with a synthesized body.

The default body synthesis is extractive: we pick the prompt + the first two
lines of the assistant response as the seed, plus a bullet list of tool
chains observed. This is blunt on purpose — it's cheap, explainable, and
doesn't hallucinate. Callers can plug in `summarize_fn(cluster) -> str` for
LLM-backed summaries.
"""
from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .db import AnalyticsDB
from .events import Event, EventType
from .memory import MemoryEntry, MemoryLayer
from .retrieval import tokenize, choose_embedding_backend, _cosine


@dataclass
class Cluster:
    key: str
    prompts: list[Event] = field(default_factory=list)
    responses: list[Event] = field(default_factory=list)
    tool_chains: list[tuple[str, str]] = field(default_factory=list)  # (tool, input_hash)
    memory_writes: list[Event] = field(default_factory=list)
    source_event_ids: list[str] = field(default_factory=list)

    @property
    def representative_text(self) -> str:
        if self.prompts:
            return self.prompts[0].payload.get("text", "")[:500]
        if self.responses:
            return self.responses[0].payload.get("text", "")[:500]
        return ""


SummarizeFn = Callable[[Cluster], str]


class CompressionEngine:
    def __init__(
        self,
        db: AnalyticsDB,
        memory: MemoryLayer,
        *,
        summarize_fn: SummarizeFn | None = None,
        session_id_for_log: str | None = None,
    ):
        self.db = db
        self.memory = memory
        self.summarize_fn = summarize_fn or _default_summarize
        self._log_session_id = session_id_for_log

    # ---- public API ----

    def run(
        self,
        *,
        since_ts: float | None = None,
        max_topics: int = 40,
        min_cluster_size: int = 1,
    ) -> dict:
        """Run a compression pass. Returns a summary dict.

        By default operates over *all* events. Pass `since_ts` to only include
        events newer than the cutoff — useful for incremental refreshes.
        """
        events = self._collect_events(since_ts)
        clusters = self._cluster(events)
        clusters = [c for c in clusters if len(c.prompts) + len(c.responses) >= min_cluster_size]
        clusters.sort(key=lambda c: -(len(c.prompts) + len(c.responses)))
        clusters = clusters[:max_topics]

        written: list[str] = []
        index_entries: list[tuple[str, str, str]] = []

        for i, cluster in enumerate(clusters):
            title = _cluster_title(cluster, fallback=f"topic_{i:02d}")
            body = self.summarize_fn(cluster)
            entry = MemoryEntry(
                name=_safe_stem(title),
                description=_cluster_description(cluster),
                type="project",
                body=body,
                source_events=cluster.source_event_ids,
            )
            path = self.memory.write_topic(entry, session_id=self._log_session_id)
            rel = Path("topics") / path.name
            index_entries.append((title, str(rel), entry.description[:100]))
            written.append(entry.name)

        self.memory.update_index(index_entries, session_id=self._log_session_id)

        summary = {
            "clusters": len(clusters),
            "topics_written": written,
            "events_considered": sum(
                len(c.prompts) + len(c.responses) + len(c.tool_chains)
                for c in clusters
            ),
            "timestamp": time.time(),
        }

        # Log the pass itself as an event so it is visible in the flight recorder.
        if self._log_session_id is not None:
            from .events import make_event
            self.memory._store and self.memory._store.append(make_event(  # type: ignore[func-returns-value]
                session_id=self._log_session_id,
                seq=self.memory._store._next_seq(self._log_session_id),  # type: ignore[union-attr]
                type=EventType.COMPRESSION_RUN,
                payload={"summary": summary},
            ))
        return summary

    # ---- collection ----

    def _collect_events(self, since_ts: float | None) -> list[Event]:
        rows = self.db._conn.execute(
            """SELECT * FROM events
               WHERE (? IS NULL OR ts >= ?)
               ORDER BY ts ASC""",
            (since_ts, since_ts or 0),
        ).fetchall()
        out: list[Event] = []
        for r in rows:
            out.append(Event(
                id=r["id"], session_id=r["session_id"], seq=r["seq"], ts=r["ts"],
                type=EventType(r["type"]),
                payload=json.loads(r["payload_json"]),
                parent_ids=tuple(json.loads(r["parent_ids_json"])),
                tokens_in=r["tokens_in"], tokens_out=r["tokens_out"],
                cached_tokens=r["cached_tokens"],
                cache_creation_tokens=r["cache_creation_tokens"],
                model=r["model"], latency_ms=r["latency_ms"],
            ))
        return out

    # ---- clustering ----

    def _cluster(self, events: list[Event]) -> list[Cluster]:
        """Group prompts and their downstream events into topical clusters.

        Try embeddings first; fall back to lexical overlap if unavailable.
        """
        # Partition by turn, mirror attribution logic.
        turns: list[tuple[Event, list[Event]]] = []
        bucket: list[Event] = []
        current_prompt: Event | None = None
        for ev in events:
            if ev.type is EventType.USER_PROMPT:
                if current_prompt is not None:
                    turns.append((current_prompt, bucket))
                current_prompt = ev
                bucket = [ev]
            elif current_prompt is not None:
                bucket.append(ev)
        if current_prompt is not None:
            turns.append((current_prompt, bucket))

        if not turns:
            return []

        # Cluster prompts by semantic similarity.
        try:
            backend = choose_embedding_backend()
            texts = [p.payload.get("text", "") for p, _ in turns]
            embeddings = backend.embed_batch(texts)
            assignments = _embedding_cluster(embeddings, threshold=0.58)
        except Exception:
            # Lexical fallback: jaccard on token sets.
            assignments = _lexical_cluster(
                [p.payload.get("text", "") for p, _ in turns],
                threshold=0.18,
            )

        groups: dict[int, Cluster] = {}
        for (prompt, turn), cid in zip(turns, assignments):
            cluster = groups.get(cid)
            if cluster is None:
                cluster = Cluster(key=f"c{cid:03d}")
                groups[cid] = cluster
            cluster.prompts.append(prompt)
            cluster.source_event_ids.append(prompt.id)
            for ev in turn[1:]:
                cluster.source_event_ids.append(ev.id)
                if ev.type is EventType.ASSISTANT_MESSAGE:
                    cluster.responses.append(ev)
                elif ev.type is EventType.POST_TOOL_USE:
                    cluster.tool_chains.append((
                        ev.payload.get("tool_name", "?"),
                        _first_line(json.dumps(ev.payload.get("output", ""))),
                    ))
                elif ev.type is EventType.MEMORY_WRITE:
                    cluster.memory_writes.append(ev)
        return list(groups.values())


# ---- cluster assignment primitives ----

def _embedding_cluster(vectors: list[list[float]], *, threshold: float) -> list[int]:
    """Single-link clustering with a cosine threshold.

    Produces deterministic assignments given the same input order.
    """
    centroids: list[list[float]] = []
    assignments: list[int] = []
    for v in vectors:
        best_id = -1
        best_sim = threshold
        for i, c in enumerate(centroids):
            sim = _cosine(v, c)
            if sim > best_sim:
                best_sim = sim
                best_id = i
        if best_id == -1:
            centroids.append(v)
            assignments.append(len(centroids) - 1)
        else:
            assignments.append(best_id)
    return assignments


def _lexical_cluster(texts: list[str], *, threshold: float) -> list[int]:
    clusters: list[set[str]] = []
    assignments: list[int] = []
    token_sets = [set(tokenize(t)) for t in texts]
    for tokens in token_sets:
        best_id = -1
        best_sim = threshold
        for i, c in enumerate(clusters):
            if not c or not tokens:
                continue
            sim = len(tokens & c) / len(tokens | c)
            if sim > best_sim:
                best_sim = sim
                best_id = i
        if best_id == -1:
            clusters.append(set(tokens))
            assignments.append(len(clusters) - 1)
        else:
            assignments.append(best_id)
            clusters[best_id] |= tokens
    return assignments


# ---- body synthesis ----

def _default_summarize(cluster: Cluster) -> str:
    """Extractive, LLM-free summarization.

    Structure:
        ## Prompts
        - first-line of prompt 1
        - first-line of prompt 2

        ## Outputs (key lines)
        - first line of response 1
        - ...

        ## Tool chains
        - bash: git status
        - read: src/foo.py
    """
    lines: list[str] = []
    if cluster.prompts:
        lines.append("## Prompts")
        for p in cluster.prompts[:8]:
            lines.append(f"- {_first_line(p.payload.get('text', ''))}")
        lines.append("")
    if cluster.responses:
        lines.append("## Key responses")
        for r in cluster.responses[:5]:
            txt = r.payload.get("text", "")
            lines.append(f"- {_first_line(txt)}")
        lines.append("")
    if cluster.tool_chains:
        tool_counts: dict[str, int] = {}
        for t, _ in cluster.tool_chains:
            tool_counts[t] = tool_counts.get(t, 0) + 1
        lines.append("## Tool usage")
        for t, n in sorted(tool_counts.items(), key=lambda kv: -kv[1])[:10]:
            lines.append(f"- {t}: {n}x")
        lines.append("")
    if cluster.memory_writes:
        lines.append("## Memory writes captured in cluster")
        for m in cluster.memory_writes[:10]:
            lines.append(f"- {m.payload.get('path')} ({m.payload.get('bytes', 0)} bytes)")
        lines.append("")
    if not lines:
        return "_empty cluster_\n"
    return "\n".join(lines)


def _cluster_title(cluster: Cluster, *, fallback: str) -> str:
    text = cluster.representative_text
    if not text.strip():
        return fallback
    # Title = first 8 words of the prompt, or first line, whichever is shorter.
    first = _first_line(text)
    words = first.split()
    if len(words) > 10:
        first = " ".join(words[:10]) + "..."
    return first.strip(" .:;,!?") or fallback


def _cluster_description(cluster: Cluster) -> str:
    nprompts = len(cluster.prompts)
    nresp = len(cluster.responses)
    ntools = len(cluster.tool_chains)
    return f"{nprompts} prompt(s), {nresp} response(s), {ntools} tool call(s)"


def _first_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:160]
    return "(empty)"


def _safe_stem(name: str) -> str:
    stem = re.sub(r"[^a-zA-Z0-9._-]+", "_", name).strip("._-")
    return stem[:64] or "topic"
