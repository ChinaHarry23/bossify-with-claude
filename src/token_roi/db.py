"""SQLite analytics layer.

The DB is **purely derived state**. It can be dropped and rebuilt from the
JSONL raw event tree at any time via `AnalyticsDB.rebuild_from(store)`.

This separation is deliberate:
    - The raw events are the audit log. They are append-only and cheap to
      trust.
    - The DB is the query engine. It is cheap to rebuild, so we tolerate
      schema churn and keep it denormalized for ROI queries.

Schema philosophy:
    - `events` table mirrors the JSONL one-row-per-event. Payload is stored
      as JSON text; SQLite's json functions are enough for our queries.
    - `memory_writes`, `retrievals`, and `attributions` are *materialized*
      views kept in real tables so dashboard queries stay sub-ms.
    - `roi_scores` stores per-event classifications. It is fully derivable
      but we cache it because scoring is the expensive path.

Every table has a `source_event_id` or equivalent back-ref so any row can be
traced to the raw event(s) that produced it. That is the audit invariant.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from .events import Event, EventType


SCHEMA_VERSION = 1


DDL = [
    """
    CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER PRIMARY KEY
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        id                    TEXT PRIMARY KEY,
        session_id            TEXT NOT NULL,
        seq                   INTEGER NOT NULL,
        ts                    REAL NOT NULL,
        type                  TEXT NOT NULL,
        payload_json          TEXT NOT NULL,
        parent_ids_json       TEXT NOT NULL,
        tokens_in             INTEGER NOT NULL DEFAULT 0,
        tokens_out            INTEGER NOT NULL DEFAULT 0,
        cached_tokens         INTEGER NOT NULL DEFAULT 0,
        cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
        model                 TEXT,
        latency_ms            INTEGER,
        -- denormalized for fast aggregation
        total_tokens          INTEGER GENERATED ALWAYS AS
            (tokens_in + tokens_out + cached_tokens + cache_creation_tokens) STORED
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_events_session_seq ON events (session_id, seq);",
    "CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);",
    "CREATE INDEX IF NOT EXISTS idx_events_type ON events (type);",

    # Materialized: one row per memory write, with its write event id so we
    # can join back. Updated as events stream in.
    """
    CREATE TABLE IF NOT EXISTS memory_writes (
        source_event_id TEXT PRIMARY KEY,
        session_id      TEXT NOT NULL,
        ts              REAL NOT NULL,
        path            TEXT NOT NULL,
        kind            TEXT,
        content_hash    TEXT,
        -- populated later by attribution
        retrieval_hits  INTEGER NOT NULL DEFAULT 0,
        last_retrieved  REAL,
        FOREIGN KEY (source_event_id) REFERENCES events(id)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_memory_writes_path ON memory_writes (path);",
    "CREATE INDEX IF NOT EXISTS idx_memory_writes_ts ON memory_writes (ts);",

    # Materialized: one row per retrieval query. Hits are JSON-encoded.
    """
    CREATE TABLE IF NOT EXISTS retrievals (
        source_event_id TEXT PRIMARY KEY,
        session_id      TEXT NOT NULL,
        ts              REAL NOT NULL,
        query           TEXT NOT NULL,
        hit_ids_json    TEXT NOT NULL,   -- JSON list of memory_write.source_event_id
        -- was the retrieval followed by a non-trivial assistant message in
        -- the same session within N events? set by attribution.
        used_downstream INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY (source_event_id) REFERENCES events(id)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_retrievals_session ON retrievals (session_id);",

    # Attribution: per prompt, the derived costs and values.
    # The *_json columns store the contribution lists so any persisted ROI
    # score can name the exact events that produced each term.
    #
    # The *_proxy columns hold retrospective value signals (file writes,
    # tool success rate) used when live signals are absent — see
    # references/roi-model.md for the exact formula.
    """
    CREATE TABLE IF NOT EXISTS attributions (
        prompt_event_id          TEXT PRIMARY KEY,
        session_id               TEXT NOT NULL,
        cost_tokens              INTEGER NOT NULL,
        durable_bytes            INTEGER NOT NULL,
        retrieval_count          INTEGER NOT NULL,
        outcome_score            REAL NOT NULL,
        reuse_score              REAL NOT NULL,
        file_write_bytes         INTEGER NOT NULL DEFAULT 0,
        tool_calls               INTEGER NOT NULL DEFAULT 0,
        tool_successes           INTEGER NOT NULL DEFAULT 0,
        cost_event_ids_json      TEXT NOT NULL DEFAULT '[]',
        memory_write_ids_json    TEXT NOT NULL DEFAULT '[]',
        retrieval_hit_ids_json   TEXT NOT NULL DEFAULT '[]',
        outcome_event_ids_json   TEXT NOT NULL DEFAULT '[]',
        file_write_event_ids_json TEXT NOT NULL DEFAULT '[]',
        FOREIGN KEY (prompt_event_id) REFERENCES events(id)
    );
    """,

    # Local-LLM session summaries — cached human-readable name + one-line
    # description per session. Cheap (one LLM call per session) and survives
    # rebuild_from. Joined by session_id into every dashboard endpoint that
    # returns sessions.
    """
    CREATE TABLE IF NOT EXISTS session_summaries (
        session_id   TEXT PRIMARY KEY,
        name         TEXT NOT NULL,
        summary      TEXT NOT NULL,
        model        TEXT NOT NULL,
        generated_at REAL NOT NULL
    );
    """,

    # Local-LLM judgments — cached per prompt. Separated from attributions
    # because they are expensive to compute (one LLM call each) and worth
    # persisting independently of attribution/scoring cycles.
    """
    CREATE TABLE IF NOT EXISTS llm_judgments (
        prompt_event_id       TEXT PRIMARY KEY,
        meaningful_value      REAL NOT NULL,
        code_quality          REAL,
        output_durability     REAL NOT NULL,
        efficiency            REAL NOT NULL,
        aggregate             REAL NOT NULL,
        reasoning             TEXT NOT NULL,
        wasteful_patterns_json TEXT NOT NULL DEFAULT '[]',
        model                 TEXT NOT NULL,
        judged_at             REAL NOT NULL,
        FOREIGN KEY (prompt_event_id) REFERENCES events(id)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_llm_meaningful ON llm_judgments (meaningful_value);",

    # ROI scores. We cache these because scoring involves global aggregation.
    """
    CREATE TABLE IF NOT EXISTS roi_scores (
        scope_kind      TEXT NOT NULL,   -- 'prompt' | 'session' | 'tool_chain' | 'memory_write'
        scope_id        TEXT NOT NULL,
        class           TEXT NOT NULL,   -- HIGH_VALUE | TRANSIENT_VALUE | LOW_VALUE | WASTED
        score           REAL NOT NULL,
        derivation_json TEXT NOT NULL,   -- full audit trail for explain
        computed_at     REAL NOT NULL,
        PRIMARY KEY (scope_kind, scope_id)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_roi_class ON roi_scores (class, score);",
]


MATERIALIZED_VIEWS_SQL = {
    # Top spenders — prompts that ate the most tokens, regardless of value.
    "top_spenders.sql": """
        SELECT
            a.prompt_event_id AS prompt_id,
            a.session_id,
            e.payload_json    AS prompt_payload,
            a.cost_tokens,
            r.class,
            r.score
        FROM attributions a
        LEFT JOIN events      e ON e.id = a.prompt_event_id
        LEFT JOIN roi_scores  r ON r.scope_kind = 'prompt' AND r.scope_id = a.prompt_event_id
        ORDER BY a.cost_tokens DESC
        LIMIT 50;
    """,

    # Orphan memory — memory writes never retrieved.
    "orphan_memory.sql": """
        SELECT mw.*, e.payload_json
        FROM memory_writes mw
        JOIN events e ON e.id = mw.source_event_id
        WHERE mw.retrieval_hits = 0
        ORDER BY mw.ts DESC;
    """,

    # Token black holes — sessions the ROI classifier flagged as LOW_VALUE
    # or WASTED that still cost meaningful tokens. Joining against
    # roi_scores (scope='session') means "black hole" tracks the current
    # classification, not an out-of-date cost-vs-memory ratio. Sessions
    # that the model promoted to HIGH_VALUE or TRANSIENT — e.g. via a
    # strong LLM verdict on file-write-heavy work — correctly drop out
    # of this chart.
    #
    # The `include_legacy_unscored` branch keeps the view useful while a
    # user is still setting up the skill (no scores yet): any session
    # over the cost threshold with no classification at all also appears,
    # because we don't yet know whether it's a black hole or not.
    "black_holes.sql": """
        SELECT
            a.session_id,
            SUM(a.cost_tokens)          AS total_cost,
            SUM(a.durable_bytes)        AS total_durable,
            SUM(a.file_write_bytes)     AS total_file_writes,
            SUM(a.retrieval_count)      AS total_reuse,
            AVG(a.outcome_score)        AS avg_outcome,
            r.class                     AS roi_class,
            r.score                     AS roi_score
        FROM attributions a
        LEFT JOIN roi_scores r
          ON r.scope_kind = 'session' AND r.scope_id = a.session_id
        GROUP BY a.session_id, r.class, r.score
        HAVING SUM(a.cost_tokens) > 10000
           AND (r.class IS NULL OR r.class IN ('LOW_VALUE', 'WASTED'))
        ORDER BY total_cost DESC
        LIMIT 25;
    """,
}


@dataclass
class SessionTotals:
    session_id: str
    event_count: int
    prompt_count: int
    tool_call_count: int
    tokens_in: int
    tokens_out: int
    cached_tokens: int
    cache_creation_tokens: int
    total_tokens: int
    memory_writes: int
    retrievals: int


class AnalyticsDB:
    """Thin SQLite wrapper. Every method is short and explicit."""

    def __init__(self, db_path: Path | str):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False lets FastAPI's worker-thread request handlers
        # share a single AnalyticsDB instance. Safe here because WAL mode +
        # `BEGIN IMMEDIATE` in txn() serialize writes at the SQLite layer, and
        # reads are allowed to run concurrently in WAL.
        self._conn = sqlite3.connect(
            self.path, isolation_level=None, check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL;")
        self._conn.execute("PRAGMA synchronous = NORMAL;")
        self._conn.execute("PRAGMA foreign_keys = ON;")

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def __enter__(self) -> "AnalyticsDB":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __del__(self) -> None:
        # Best-effort cleanup so short-lived CLI uses don't leak a sqlite
        # connection. We intentionally catch everything — __del__ must not raise.
        try:
            self.close()
        except Exception:
            pass

    @contextmanager
    def txn(self) -> Iterator[sqlite3.Connection]:
        """Explicit transactions for multi-row upserts."""
        self._conn.execute("BEGIN IMMEDIATE;")
        try:
            yield self._conn
            self._conn.execute("COMMIT;")
        except Exception:
            self._conn.execute("ROLLBACK;")
            raise

    # ---- schema ----

    def migrate(self) -> None:
        for stmt in DDL:
            self._conn.execute(stmt)
        cur = self._conn.execute("SELECT version FROM schema_version;").fetchone()
        if cur is None:
            self._conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
        # Additive column migrations — CREATE TABLE IF NOT EXISTS can't add
        # columns to an existing table, so we check PRAGMA table_info and
        # ALTER when needed. Idempotent.
        self._ensure_column("session_summaries", "project_slug", "TEXT")
        self._ensure_column("session_summaries", "employee_id",  "TEXT")

    def _ensure_column(self, table: str, column: str, coltype: str) -> None:
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        if any(r["name"] == column for r in rows):
            return
        self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")

    # ---- ingest ----

    def upsert_event(self, ev: Event) -> None:
        """Insert an event. Silently ignores duplicates (same id)."""
        self._conn.execute(
            """
            INSERT OR IGNORE INTO events
                (id, session_id, seq, ts, type, payload_json, parent_ids_json,
                 tokens_in, tokens_out, cached_tokens, cache_creation_tokens,
                 model, latency_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ev.id, ev.session_id, ev.seq, ev.ts, ev.type.value,
                json.dumps(ev.payload, ensure_ascii=False),
                json.dumps(list(ev.parent_ids)),
                ev.tokens_in, ev.tokens_out, ev.cached_tokens, ev.cache_creation_tokens,
                ev.model, ev.latency_ms,
            ),
        )
        # Keep materialized tables in sync with this single event.
        if ev.type is EventType.MEMORY_WRITE:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO memory_writes
                    (source_event_id, session_id, ts, path, kind, content_hash)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    ev.id, ev.session_id, ev.ts,
                    ev.payload.get("path"),
                    ev.payload.get("kind"),
                    ev.payload.get("content_hash"),
                ),
            )
        elif ev.type is EventType.RETRIEVAL_RESULT:
            hit_ids = [h.get("memory_write_id") for h in (ev.payload.get("hits") or [])]
            self._conn.execute(
                """
                INSERT OR REPLACE INTO retrievals
                    (source_event_id, session_id, ts, query, hit_ids_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    ev.id, ev.session_id, ev.ts,
                    ev.payload.get("query"),
                    json.dumps([h for h in hit_ids if h]),
                ),
            )

    def rebuild_from(self, events: Iterable[Event]) -> int:
        """Re-ingest events and reset derived tables. Returns count.

        Derived tables (roi_scores, attributions, retrievals, memory_writes)
        are cleared so they get recomputed on the next score run.

        Events themselves are NOT deleted — they are append-only and
        content-addressed by id, so INSERT OR IGNORE in `upsert_event` is
        idempotent. Preserving events also preserves:
            - foreign keys from llm_judgments (expensive to regenerate)
            - any future derived tables that want to stick around
        """
        with self.txn() as c:
            c.execute("DELETE FROM roi_scores;")
            c.execute("DELETE FROM attributions;")
            c.execute("DELETE FROM retrievals;")
            c.execute("DELETE FROM memory_writes;")
        n = 0
        with self.txn():
            for ev in events:
                self.upsert_event(ev)
                n += 1
        return n

    # ---- queries ----

    def session_totals(self, session_id: str) -> SessionTotals | None:
        row = self._conn.execute(
            """
            SELECT
                session_id,
                COUNT(*)                                                AS event_count,
                SUM(type = 'user_prompt')                               AS prompt_count,
                SUM(type = 'post_tool_use')                             AS tool_call_count,
                SUM(tokens_in)                                          AS tokens_in,
                SUM(tokens_out)                                         AS tokens_out,
                SUM(cached_tokens)                                      AS cached_tokens,
                SUM(cache_creation_tokens)                              AS cache_creation_tokens,
                SUM(total_tokens)                                       AS total_tokens,
                SUM(type = 'memory_write')                              AS memory_writes,
                SUM(type = 'retrieval_query')                           AS retrievals
            FROM events
            WHERE session_id = ?
            GROUP BY session_id
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return SessionTotals(
            session_id=row["session_id"],
            event_count=row["event_count"] or 0,
            prompt_count=row["prompt_count"] or 0,
            tool_call_count=row["tool_call_count"] or 0,
            tokens_in=row["tokens_in"] or 0,
            tokens_out=row["tokens_out"] or 0,
            cached_tokens=row["cached_tokens"] or 0,
            cache_creation_tokens=row["cache_creation_tokens"] or 0,
            total_tokens=row["total_tokens"] or 0,
            memory_writes=row["memory_writes"] or 0,
            retrievals=row["retrievals"] or 0,
        )

    def iter_session_events(self, session_id: str) -> Iterator[Event]:
        rows = self._conn.execute(
            """SELECT * FROM events WHERE session_id = ? ORDER BY seq ASC""",
            (session_id,),
        )
        for r in rows:
            yield Event(
                id=r["id"], session_id=r["session_id"], seq=r["seq"], ts=r["ts"],
                type=EventType(r["type"]),
                payload=json.loads(r["payload_json"]),
                parent_ids=tuple(json.loads(r["parent_ids_json"])),
                tokens_in=r["tokens_in"], tokens_out=r["tokens_out"],
                cached_tokens=r["cached_tokens"],
                cache_creation_tokens=r["cache_creation_tokens"],
                model=r["model"], latency_ms=r["latency_ms"],
            )

    def prompts_in_session(self, session_id: str) -> list[tuple[str, str]]:
        """Return (event_id, prompt_text) for every user prompt in a session."""
        rows = self._conn.execute(
            """SELECT id, payload_json FROM events
               WHERE session_id = ? AND type = 'user_prompt' ORDER BY seq""",
            (session_id,),
        ).fetchall()
        return [(r["id"], json.loads(r["payload_json"]).get("text", "")) for r in rows]

    def all_sessions(self) -> list[str]:
        rows = self._conn.execute(
            """SELECT DISTINCT session_id FROM events ORDER BY session_id"""
        ).fetchall()
        return [r["session_id"] for r in rows]

    def sessions_since(self, cutoff_ts: float) -> list[str]:
        rows = self._conn.execute(
            """SELECT DISTINCT session_id FROM events
               WHERE ts >= ? ORDER BY session_id""",
            (cutoff_ts,),
        ).fetchall()
        return [r["session_id"] for r in rows]

    # ---- materialized updaters ----

    def increment_memory_hit(self, memory_write_id: str, at_ts: float) -> None:
        """Called by attribution when a memory write is observed in a retrieval hit."""
        self._conn.execute(
            """UPDATE memory_writes
                   SET retrieval_hits = retrieval_hits + 1,
                       last_retrieved = MAX(COALESCE(last_retrieved, 0), ?)
                   WHERE source_event_id = ?""",
            (at_ts, memory_write_id),
        )

    def mark_retrieval_used(self, retrieval_event_id: str) -> None:
        self._conn.execute(
            """UPDATE retrievals SET used_downstream = 1 WHERE source_event_id = ?""",
            (retrieval_event_id,),
        )

    def upsert_attribution(
        self,
        *,
        prompt_event_id: str,
        session_id: str,
        cost_tokens: int,
        durable_bytes: int,
        retrieval_count: int,
        outcome_score: float,
        reuse_score: float,
        file_write_bytes: int = 0,
        tool_calls: int = 0,
        tool_successes: int = 0,
        cost_event_ids: list[str] | None = None,
        memory_write_ids: list[str] | None = None,
        retrieval_hit_ids: list[str] | None = None,
        outcome_event_ids: list[str] | None = None,
        file_write_event_ids: list[str] | None = None,
    ) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO attributions
                (prompt_event_id, session_id, cost_tokens, durable_bytes,
                 retrieval_count, outcome_score, reuse_score,
                 file_write_bytes, tool_calls, tool_successes,
                 cost_event_ids_json, memory_write_ids_json,
                 retrieval_hit_ids_json, outcome_event_ids_json,
                 file_write_event_ids_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (prompt_event_id, session_id, cost_tokens, durable_bytes,
             retrieval_count, outcome_score, reuse_score,
             file_write_bytes, tool_calls, tool_successes,
             json.dumps(cost_event_ids or []),
             json.dumps(memory_write_ids or []),
             json.dumps(retrieval_hit_ids or []),
             json.dumps(outcome_event_ids or []),
             json.dumps(file_write_event_ids or [])),
        )

    def upsert_roi_score(
        self,
        *,
        scope_kind: str,
        scope_id: str,
        roi_class: str,
        score: float,
        derivation: dict,
        computed_at: float,
    ) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO roi_scores
                (scope_kind, scope_id, class, score, derivation_json, computed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (scope_kind, scope_id, roi_class, score,
             json.dumps(derivation, default=str), computed_at),
        )

    def get_roi_score(self, scope_kind: str, scope_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            """SELECT * FROM roi_scores WHERE scope_kind = ? AND scope_id = ?""",
            (scope_kind, scope_id),
        ).fetchone()

    # ---- Session summary I/O ----

    def upsert_session_summary(self, summary) -> None:
        """Persist a SessionSummary (from llm_judge.SessionSummary)."""
        self._conn.execute(
            """
            INSERT OR REPLACE INTO session_summaries
                (session_id, name, summary, model, generated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (summary.session_id, summary.name, summary.summary,
             summary.model, summary.generated_at),
        )

    def get_session_name(self, session_id: str) -> str | None:
        row = self._conn.execute(
            """SELECT name FROM session_summaries WHERE session_id = ?""",
            (session_id,),
        ).fetchone()
        return row["name"] if row else None

    def upsert_session_metadata(
        self,
        session_id: str,
        *,
        project_slug: str | None = None,
        employee_id: str | None = None,
    ) -> None:
        """Populate project_slug/employee_id without overwriting an existing
        LLM-generated name or summary. Called from the importer once per
        ingested JSONL file, well before `token-roi name-sessions` runs.
        """
        existing = self._conn.execute(
            """SELECT name, summary, model, generated_at
                 FROM session_summaries WHERE session_id = ?""",
            (session_id,),
        ).fetchone()
        if existing is None:
            # Insert a minimal placeholder row so later joins on
            # employee_id work even before naming.
            import time as _time
            self._conn.execute(
                """
                INSERT OR REPLACE INTO session_summaries
                    (session_id, name, summary, model, generated_at,
                     project_slug, employee_id)
                VALUES (?, '', '', 'metadata-only', ?, ?, ?)
                """,
                (session_id, _time.time(), project_slug, employee_id),
            )
        else:
            self._conn.execute(
                """UPDATE session_summaries
                      SET project_slug = COALESCE(?, project_slug),
                          employee_id  = COALESCE(?, employee_id)
                    WHERE session_id = ?""",
                (project_slug, employee_id, session_id),
            )

    def session_names(self) -> dict[str, dict]:
        """Return {session_id: {name, summary, model}} for every named session."""
        rows = self._conn.execute(
            """SELECT session_id, name, summary, model FROM session_summaries"""
        ).fetchall()
        return {
            r["session_id"]: {
                "name":    r["name"],
                "summary": r["summary"],
                "model":   r["model"],
            }
            for r in rows
        }

    # ---- LLM judgment I/O ----

    def upsert_llm_judgment(self, judgment) -> None:
        """Persist a Judgment (from llm_judge.Judgment)."""
        self._conn.execute(
            """
            INSERT OR REPLACE INTO llm_judgments
                (prompt_event_id, meaningful_value, code_quality,
                 output_durability, efficiency, aggregate,
                 reasoning, wasteful_patterns_json,
                 model, judged_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                judgment.prompt_event_id,
                judgment.meaningful_value,
                judgment.code_quality,
                judgment.output_durability,
                judgment.efficiency,
                judgment.aggregate,
                judgment.reasoning,
                json.dumps(list(judgment.wasteful_patterns or [])),
                judgment.model,
                judgment.judged_at,
            ),
        )

    def get_llm_judgment(self, prompt_event_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            """SELECT * FROM llm_judgments WHERE prompt_event_id = ?""",
            (prompt_event_id,),
        ).fetchone()

    def iter_llm_judgments(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            """SELECT * FROM llm_judgments ORDER BY judged_at DESC"""
        ).fetchall()

    def llm_judgments_summary(self) -> dict:
        row = self._conn.execute(
            """
            SELECT COUNT(*)                AS n,
                   AVG(meaningful_value)   AS avg_meaningful,
                   AVG(output_durability)  AS avg_durability,
                   AVG(efficiency)         AS avg_efficiency,
                   AVG(aggregate)          AS avg_aggregate
              FROM llm_judgments
            """
        ).fetchone()
        return {
            "count":          row["n"] or 0,
            "avg_meaningful": row["avg_meaningful"],
            "avg_durability": row["avg_durability"],
            "avg_efficiency": row["avg_efficiency"],
            "avg_aggregate":  row["avg_aggregate"],
        }

    def roi_summary(self) -> dict[str, int]:
        rows = self._conn.execute(
            """SELECT class, COUNT(*) AS n FROM roi_scores GROUP BY class"""
        ).fetchall()
        return {r["class"]: r["n"] for r in rows}

    # ---- employee-oriented rollups ----

    def employees_with_stats(self, registry) -> list[dict]:
        """One dict per known employee with team/productivity rollups.

        `registry` is an EmployeeRegistry — we pull display metadata
        (name, role, team) from it rather than storing a copy in the DB,
        so editing employees.json is authoritative.

        Sessions with NULL employee_id are bucketed under the registry's
        default employee so nothing is orphaned in the UI.
        """
        default_id = registry.default().id
        rows = self._conn.execute(
            """
            SELECT
                COALESCE(s.employee_id, ?)                AS employee_id,
                a.session_id,
                SUM(a.cost_tokens)                        AS cost,
                SUM(a.file_write_bytes)                   AS file_bytes,
                SUM(a.durable_bytes)                      AS durable_bytes,
                SUM(a.tool_calls)                         AS tools,
                SUM(a.tool_successes)                     AS tool_ok,
                MAX(e_ts.last_ts)                         AS last_ts,
                r.class                                    AS session_class,
                r.score                                    AS session_score,
                AVG(j.aggregate)                           AS avg_llm,
                AVG(j.efficiency)                          AS avg_eff,
                AVG(j.meaningful_value)                    AS avg_meaningful,
                GROUP_CONCAT(j.wasteful_patterns_json, '\x1e') AS waste_blob
            FROM attributions a
            LEFT JOIN session_summaries s ON s.session_id = a.session_id
            LEFT JOIN roi_scores r
                   ON r.scope_kind = 'session' AND r.scope_id = a.session_id
            LEFT JOIN llm_judgments j
                   ON j.prompt_event_id = a.prompt_event_id
            LEFT JOIN (
                SELECT session_id, MAX(ts) AS last_ts FROM events GROUP BY session_id
            ) e_ts ON e_ts.session_id = a.session_id
            GROUP BY a.session_id
            """,
            (default_id,),
        ).fetchall()

        # Bucket rows per employee.
        buckets: dict[str, list[sqlite3.Row]] = {}
        for r in rows:
            buckets.setdefault(r["employee_id"], []).append(r)

        # Also include employees with zero sessions so the dashboard shows them.
        for e in registry.all():
            buckets.setdefault(e.id, [])

        out: list[dict] = []
        for emp_id, group in buckets.items():
            emp = registry.get(emp_id) or registry.default()
            total_cost = sum((r["cost"] or 0) for r in group)
            total_file = sum((r["file_bytes"] or 0) for r in group)
            total_tools = sum((r["tools"] or 0) for r in group)
            total_ok = sum((r["tool_ok"] or 0) for r in group)
            session_count = len(group)
            last_ts_vals = [r["last_ts"] for r in group if r["last_ts"] is not None]
            last_ts = max(last_ts_vals) if last_ts_vals else None

            # ROI distribution at the session level (per-session class).
            roi_counts = {"HIGH_VALUE": 0, "TRANSIENT_VALUE": 0,
                          "LOW_VALUE": 0, "WASTED": 0, "UNSCORED": 0}
            for r in group:
                cls = r["session_class"] or "UNSCORED"
                roi_counts[cls] = roi_counts.get(cls, 0) + 1

            # LLM aggregates — prompt-weighted, per employee.
            avg_llm_vals = [r["avg_llm"] for r in group if r["avg_llm"] is not None]
            avg_eff_vals = [r["avg_eff"] for r in group if r["avg_eff"] is not None]
            avg_mea_vals = [r["avg_meaningful"] for r in group if r["avg_meaningful"] is not None]
            avg_llm = sum(avg_llm_vals) / len(avg_llm_vals) if avg_llm_vals else None
            avg_eff = sum(avg_eff_vals) / len(avg_eff_vals) if avg_eff_vals else None
            avg_mea = sum(avg_mea_vals) / len(avg_mea_vals) if avg_mea_vals else None

            # Waste-pattern frequency roll-up. Count identical strings;
            # surface the top 5 so the card can show them as chips.
            pattern_counts: dict[str, int] = {}
            for r in group:
                blob = r["waste_blob"] or ""
                for chunk in blob.split("\x1e"):
                    if not chunk.strip():
                        continue
                    try:
                        for p in json.loads(chunk):
                            if isinstance(p, str) and p.strip():
                                pattern_counts[p] = pattern_counts.get(p, 0) + 1
                    except (json.JSONDecodeError, TypeError):
                        continue
            top_waste = sorted(pattern_counts.items(), key=lambda kv: -kv[1])[:5]

            out.append({
                "id":             emp.id,
                "name":           emp.name,
                "role":           emp.role,
                "team":           emp.team,
                "session_count":  session_count,
                "total_cost":     total_cost,
                "file_write_bytes": total_file,
                "tool_calls":     total_tools,
                "tool_successes": total_ok,
                "avg_llm":        avg_llm,
                "avg_efficiency": avg_eff,
                "avg_meaningful": avg_mea,
                "roi_counts":     roi_counts,
                "top_waste":      [{"pattern": p, "count": n} for p, n in top_waste],
                "last_active":    last_ts,
            })

        # Sort biggest spenders first — bosses want the outliers visible.
        out.sort(key=lambda d: (-d["total_cost"], d["name"].lower()))
        return out

    def sessions_for_employee(self, employee_id: str, registry) -> list[dict]:
        """Per-session rows for one employee, with ROI class + session name."""
        default_id = registry.default().id
        rows = self._conn.execute(
            """
            SELECT
                s.session_id,
                s.name                                     AS name,
                s.summary                                  AS summary,
                COALESCE(s.employee_id, ?)                 AS employee_id,
                SUM(a.cost_tokens)                         AS cost,
                SUM(a.file_write_bytes)                    AS file_bytes,
                SUM(a.tool_calls)                          AS tools,
                r.class                                     AS roi_class,
                r.score                                     AS roi_score,
                AVG(j.aggregate)                            AS avg_llm,
                AVG(j.efficiency)                           AS avg_eff
            FROM attributions a
            LEFT JOIN session_summaries s ON s.session_id = a.session_id
            LEFT JOIN roi_scores r
                   ON r.scope_kind = 'session' AND r.scope_id = a.session_id
            LEFT JOIN llm_judgments j
                   ON j.prompt_event_id = a.prompt_event_id
            GROUP BY a.session_id, s.name, s.summary, s.employee_id, r.class, r.score
            HAVING COALESCE(s.employee_id, ?) = ?
            ORDER BY cost DESC
            """,
            (default_id, default_id, employee_id),
        ).fetchall()
        return [
            {
                "session_id": r["session_id"],
                "name":       r["name"],
                "summary":    r["summary"],
                "cost":       r["cost"] or 0,
                "file_bytes": r["file_bytes"] or 0,
                "tools":      r["tools"] or 0,
                "roi_class":  r["roi_class"],
                "roi_score":  r["roi_score"],
                "avg_llm":    r["avg_llm"],
                "avg_eff":    r["avg_eff"],
            }
            for r in rows
        ]

    def team_waste_patterns(self, limit: int = 10) -> list[dict]:
        """Cross-team most-frequent waste patterns.

        Bosses use this to spot systemic issues (e.g., "everyone on my team
        is doing file_rewrite_ratio > 2 — we need a prompt-engineering
        training session").
        """
        rows = self._conn.execute(
            """SELECT wasteful_patterns_json FROM llm_judgments"""
        ).fetchall()
        counts: dict[str, int] = {}
        for r in rows:
            try:
                for p in json.loads(r["wasteful_patterns_json"] or "[]"):
                    if isinstance(p, str) and p.strip():
                        counts[p] = counts.get(p, 0) + 1
            except (json.JSONDecodeError, TypeError):
                continue
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:limit]
        return [{"pattern": p, "count": n} for p, n in top]

    # ---- materialized view access ----

    @staticmethod
    def view_sql(name: str) -> str:
        try:
            return MATERIALIZED_VIEWS_SQL[name]
        except KeyError:
            raise KeyError(f"no such view: {name}. Known: {list(MATERIALIZED_VIEWS_SQL)}")

    def run_view(self, name: str) -> list[sqlite3.Row]:
        return self._conn.execute(self.view_sql(name)).fetchall()
