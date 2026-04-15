"""FastAPI dashboard.

A purely local web UI with three primary views:

    /                   overview: ROI distribution, top spenders, totals
    /sessions           session list with totals + ROI class
    /sessions/<id>      flight recorder for one session
    /memory             memory entries with hit counts + ROI class
    /api/*              JSON endpoints consumed by the frontend

No external CDN calls. All JS + CSS is served from `static/`. The template
is a single self-contained file so the dashboard works on an air-gapped
machine.

Note: this module intentionally does NOT use `from __future__ import annotations`.
FastAPI introspects route handler signatures to decide what each parameter is
(path, query, body, Request, etc.). With lazy annotations, a locally-imported
`Request` type would be unresolvable against the module globals — FastAPI
would then misclassify the `request` parameter as a required query string
and every request to `/` would fail with HTTP 422.
"""
import json
from pathlib import Path

from ..attribution import AttributionGraph
from ..db import AnalyticsDB
from ..employees import EmployeeRegistry
from ..events import EventType
from ..i18n import all_strings, get_locale, SUPPORTED_LOCALES
from ..memory import MemoryLayer
from ..replay import ReplayOptions, Replayer
from ..retrieval import RetrievalIndex
from ..roi import ROIClassifier
from ..storage import EventStore


def make_app(data_dir: Path):
    """Construct the FastAPI app. Imported lazily so --help stays cheap."""
    try:
        from fastapi import FastAPI, HTTPException, Request
        from fastapi.responses import HTMLResponse, JSONResponse
        from fastapi.staticfiles import StaticFiles
        from fastapi.templating import Jinja2Templates
    except ImportError as e:  # pragma: no cover
        raise ImportError(
            "fastapi required for dashboard. pip install 'bossify-with-claude[dashboard]'"
        ) from e

    data_dir = Path(data_dir).resolve()
    db = AnalyticsDB(data_dir / "analytics" / "roi.db")
    db.migrate()
    store = EventStore(data_dir)
    memory = MemoryLayer(data_dir / "memory")
    # Constructed once per dashboard process. If the boss edits
    # data/employees.json they need to restart the dashboard — acceptable
    # for a local tool and simpler than hot-reloading.
    registry = EmployeeRegistry(data_dir)

    here = Path(__file__).resolve().parent
    templates = Jinja2Templates(directory=str(here / "templates"))

    app = FastAPI(title="bossify-with-claude")
    app.mount("/static", StaticFiles(directory=str(here / "static")), name="static")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        # Return 204 instead of 404 so browser devtools stay quiet.
        from fastapi.responses import Response
        return Response(status_code=204)

    @app.get("/api/i18n")
    def api_i18n(locale: str | None = None):
        """Return the translation dict for the requested (or default) locale.

        The frontend calls this exactly once at bootstrap and caches the
        result client-side, so the per-string lookup cost is zero.
        """
        loc = locale if locale in SUPPORTED_LOCALES else get_locale()
        return {
            "locale":    loc,
            "supported": list(SUPPORTED_LOCALES),
            "strings":   all_strings(loc),
        }

    # ---- HTML ----

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        summary = db.roi_summary()
        sessions = db.all_sessions()
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "roi_summary": summary,
                "session_count": len(sessions),
                "data_dir": str(data_dir),
                "locale":    get_locale(),
            },
        )

    # ---- JSON API ----

    @app.get("/api/roi/summary")
    def api_roi_summary():
        return db.roi_summary()

    @app.get("/api/sessions")
    def api_sessions():
        names = db.session_names()
        out = []
        for sid in db.all_sessions():
            t = db.session_totals(sid)
            if t is None:
                continue
            row = db.get_roi_score("session", sid)
            name_info = names.get(sid, {})
            out.append({
                "session_id": sid,
                "name":       name_info.get("name"),
                "summary":    name_info.get("summary"),
                "event_count": t.event_count,
                "prompts": t.prompt_count,
                "tools": t.tool_call_count,
                "tokens_in": t.tokens_in,
                "tokens_out": t.tokens_out,
                "cached_tokens": t.cached_tokens,
                "memory_writes": t.memory_writes,
                "retrievals": t.retrievals,
                "total_tokens": t.total_tokens,
                "roi_class": row["class"] if row else None,
                "roi_score": row["score"] if row else None,
            })
        out.sort(key=lambda r: r["total_tokens"], reverse=True)
        return out

    @app.get("/api/sessions/{session_id}")
    def api_session(session_id: str):
        """Rich session detail used by the drill-down modal.

        Joins:
          - session_summaries (name + one-line summary)
          - roi_scores scope='session'    (overall class + score + derivation)
          - attributions + roi_scores scope='prompt'  (per-prompt breakdown)
          - llm_judgments                 (per-prompt LLM verdict + reasoning)
          - events                         (file writes, tool counts)
        """
        t = db.session_totals(session_id)
        if t is None:
            raise HTTPException(404, f"no such session: {session_id}")

        summary_info = db.session_names().get(session_id) or {}
        session_roi = db.get_roi_score("session", session_id)
        session_derivation = None
        if session_roi is not None:
            try:
                session_derivation = json.loads(session_roi["derivation_json"])
            except Exception:
                session_derivation = None

        prompts = db._conn.execute(
            """
            SELECT a.prompt_event_id, a.cost_tokens, a.durable_bytes,
                   a.file_write_bytes, a.tool_calls, a.tool_successes,
                   a.retrieval_count, a.outcome_score, a.reuse_score,
                   e.payload_json, e.ts,
                   r.class, r.score, r.derivation_json,
                   j.meaningful_value, j.code_quality, j.output_durability,
                   j.efficiency, j.aggregate, j.reasoning, j.wasteful_patterns_json,
                   j.model AS judge_model
            FROM attributions a
            LEFT JOIN events         e ON e.id = a.prompt_event_id
            LEFT JOIN roi_scores     r ON r.scope_kind = 'prompt' AND r.scope_id = a.prompt_event_id
            LEFT JOIN llm_judgments  j ON j.prompt_event_id = a.prompt_event_id
            WHERE a.session_id = ?
            ORDER BY a.cost_tokens DESC
            """,
            (session_id,),
        ).fetchall()

        prompt_items = []
        for r in prompts:
            text = json.loads(r["payload_json"]).get("text", "") if r["payload_json"] else ""
            prompt_items.append({
                "id":                 r["prompt_event_id"],
                "text":               text[:600],
                "text_full_length":   len(text),
                "ts":                 r["ts"],
                "cost_tokens":        r["cost_tokens"],
                "durable_bytes":      r["durable_bytes"],
                "file_write_bytes":   r["file_write_bytes"] or 0,
                "tool_calls":         r["tool_calls"] or 0,
                "tool_successes":     r["tool_successes"] or 0,
                "retrieval_count":    r["retrieval_count"],
                "outcome_score":      r["outcome_score"],
                "reuse_score":        r["reuse_score"],
                "class":              r["class"],
                "score":              r["score"],
                "llm": ({
                    "meaningful_value":  r["meaningful_value"],
                    "code_quality":      r["code_quality"],
                    "output_durability": r["output_durability"],
                    "efficiency":        r["efficiency"],
                    "aggregate":         r["aggregate"],
                    "reasoning":         r["reasoning"],
                    "wasteful_patterns": (
                        json.loads(r["wasteful_patterns_json"])
                        if r["wasteful_patterns_json"] else []
                    ),
                    "model":             r["judge_model"],
                } if r["meaningful_value"] is not None else None),
            })

        top_files = db._conn.execute(
            """
            SELECT json_extract(payload_json, '$.path')  AS path,
                   json_extract(payload_json, '$.bytes') AS bytes,
                   COUNT(*)                              AS writes
            FROM events
            WHERE session_id = ? AND type = 'file_write'
            GROUP BY path
            ORDER BY SUM(json_extract(payload_json, '$.bytes')) DESC
            LIMIT 15
            """,
            (session_id,),
        ).fetchall()

        tools = db._conn.execute(
            """
            SELECT json_extract(payload_json, '$.tool_name') AS name,
                   COUNT(*) AS n,
                   SUM(json_extract(payload_json, '$.success') = 0) AS errors
            FROM events
            WHERE session_id = ? AND type = 'post_tool_use'
            GROUP BY name
            ORDER BY n DESC
            """,
            (session_id,),
        ).fetchall()

        return {
            "session_id": session_id,
            "name":       summary_info.get("name"),
            "summary":    summary_info.get("summary"),
            "roi_class":  session_roi["class"] if session_roi else None,
            "roi_score":  session_roi["score"] if session_roi else None,
            "roi_derivation": session_derivation,
            "totals": {
                "event_count":   t.event_count,
                "prompts":       t.prompt_count,
                "tools":         t.tool_call_count,
                "tokens_in":     t.tokens_in,
                "tokens_out":    t.tokens_out,
                "cache_read":    t.cached_tokens,
                "cache_create":  t.cache_creation_tokens,
                "total_tokens":  t.total_tokens,
                "memory_writes": t.memory_writes,
                "retrievals":    t.retrievals,
            },
            "prompts":   prompt_items,
            "top_files": [{"path": r["path"],
                           "bytes": int(r["bytes"] or 0),
                           "writes": r["writes"]}
                          for r in top_files if r["path"]],
            "tools":     [{"name": r["name"] or "unknown",
                           "count": r["n"],
                           "errors": r["errors"] or 0}
                          for r in tools],
        }

    @app.get("/api/sessions/{session_id}/replay")
    def api_replay(session_id: str):
        import io
        buf = io.StringIO()
        replayer = Replayer(store)
        replayer.replay_session(
            session_id,
            opts=ReplayOptions(mode="jsonl", show_payload=False),
            out=buf,
        )
        events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
        return events

    @app.get("/api/memory")
    def api_memory():
        rows = db._conn.execute(
            """
            SELECT mw.*, r.class, r.score
            FROM memory_writes mw
            LEFT JOIN roi_scores r ON r.scope_kind = 'memory_write' AND r.scope_id = mw.source_event_id
            ORDER BY mw.retrieval_hits DESC, mw.ts DESC
            LIMIT 200
            """
        ).fetchall()
        return [
            {
                "id": r["source_event_id"],
                "path": r["path"],
                "kind": r["kind"],
                "ts": r["ts"],
                "retrieval_hits": r["retrieval_hits"],
                "last_retrieved": r["last_retrieved"],
                "class": r["class"],
                "score": r["score"],
            }
            for r in rows
        ]

    @app.get("/api/top-spenders")
    def api_top_spenders():
        rows = db.run_view("top_spenders.sql")
        names = db.session_names()
        return [
            {
                "prompt_id": r["prompt_id"],
                "session_id": r["session_id"],
                "session_name": names.get(r["session_id"], {}).get("name"),
                "text": json.loads(r["prompt_payload"] or "{}").get("text", "")[:280],
                "cost_tokens": r["cost_tokens"],
                "class": r["class"],
                "score": r["score"],
            }
            for r in rows
        ]

    @app.get("/api/black-holes")
    def api_black_holes():
        """Sessions actually classified LOW_VALUE or WASTED, ranked by cost.

        The view already filters by roi_scores.class, so a session that
        scored HIGH_VALUE or TRANSIENT won't appear here even if its
        raw cost is high — by definition such a session is not a
        black hole.
        """
        rows = db.run_view("black_holes.sql")
        names = db.session_names()
        out = []
        for r in rows:
            d = dict(r)
            d["name"] = names.get(d.get("session_id"), {}).get("name")
            out.append(d)
        return out

    @app.get("/api/orphan-memory")
    def api_orphan_memory():
        rows = db.run_view("orphan_memory.sql")
        return [
            {
                "path": r["path"],
                "kind": r["kind"],
                "ts": r["ts"],
                "bytes": json.loads(r["payload_json"]).get("bytes", 0),
            }
            for r in rows
        ]

    @app.get("/api/kpis")
    def api_kpis():
        """Single-shot fetch of the hero-row KPIs."""
        row = db._conn.execute(
            """
            SELECT
                COUNT(DISTINCT session_id)  AS sessions,
                COUNT(*)                    AS events,
                SUM(tokens_in)              AS tokens_in,
                SUM(tokens_out)             AS tokens_out,
                SUM(cached_tokens)          AS cache_read,
                SUM(cache_creation_tokens)  AS cache_create,
                SUM(total_tokens)           AS total_tokens,
                SUM(type = 'memory_write')  AS memory_writes,
                SUM(type = 'retrieval_query') AS retrievals,
                SUM(type = 'post_tool_use') AS tool_calls
            FROM events
            """
        ).fetchone()
        roi = db.roi_summary()
        return {
            "sessions":      row["sessions"] or 0,
            "events":        row["events"] or 0,
            "tokens_in":     row["tokens_in"] or 0,
            "tokens_out":    row["tokens_out"] or 0,
            "cache_read":    row["cache_read"] or 0,
            "cache_create":  row["cache_create"] or 0,
            "total_tokens":  row["total_tokens"] or 0,
            "memory_writes": row["memory_writes"] or 0,
            "retrievals":    row["retrievals"] or 0,
            "tool_calls":    row["tool_calls"] or 0,
            "high_value":    roi.get("HIGH_VALUE", 0),
            "transient":     roi.get("TRANSIENT_VALUE", 0),
            "low_value":     roi.get("LOW_VALUE", 0),
            "wasted":        roi.get("WASTED", 0),
        }

    @app.get("/api/cost-breakdown")
    def api_cost_breakdown():
        """Per-session token breakdown: in / out / cache_read / cache_create.

        Used by the stacked-bar chart so the user can see at a glance where
        each session's cost is concentrated — cache-dominated sessions
        look very different from output-heavy sessions.
        """
        rows = db._conn.execute(
            """
            SELECT
                session_id,
                SUM(tokens_in)              AS tokens_in,
                SUM(tokens_out)             AS tokens_out,
                SUM(cached_tokens)          AS cache_read,
                SUM(cache_creation_tokens)  AS cache_create
            FROM events
            GROUP BY session_id
            ORDER BY (tokens_in + tokens_out + cached_tokens + cache_creation_tokens) DESC
            LIMIT 20
            """
        ).fetchall()
        names = db.session_names()
        return [
            {
                "session_id":   r["session_id"],
                "name":         names.get(r["session_id"], {}).get("name"),
                "tokens_in":    r["tokens_in"] or 0,
                "tokens_out":   r["tokens_out"] or 0,
                "cache_read":   r["cache_read"] or 0,
                "cache_create": r["cache_create"] or 0,
            }
            for r in rows
        ]

    @app.get("/api/tool-usage")
    def api_tool_usage():
        """Count of tool calls by tool_name. Drives the treemap."""
        rows = db._conn.execute(
            """
            SELECT
                json_extract(payload_json, '$.tool_name') AS tool,
                COUNT(*)                                  AS n,
                SUM(CASE WHEN json_extract(payload_json, '$.success') = 0
                         THEN 1 ELSE 0 END)               AS errors
            FROM events
            WHERE type = 'post_tool_use'
            GROUP BY tool
            ORDER BY n DESC
            """
        ).fetchall()
        return [{"name": r["tool"] or "unknown",
                 "value": r["n"],
                 "errors": r["errors"] or 0} for r in rows]

    @app.get("/api/timeline")
    def api_timeline(bucket_minutes: int = 30):
        """Bucket events by time window; emit token flow + counts per bucket.

        Drives the area chart. Buckets chosen client-side via the
        `bucket_minutes` query string — 30min is a decent default for
        multi-hour sessions.
        """
        bucket_s = max(1, bucket_minutes) * 60
        rows = db._conn.execute(
            f"""
            SELECT
                CAST(ts / {bucket_s} AS INTEGER) * {bucket_s} AS bucket,
                SUM(tokens_in)              AS tokens_in,
                SUM(tokens_out)             AS tokens_out,
                SUM(cached_tokens)          AS cache_read,
                SUM(cache_creation_tokens)  AS cache_create,
                COUNT(*)                    AS events,
                SUM(type = 'user_prompt')   AS prompts
            FROM events
            GROUP BY bucket
            ORDER BY bucket ASC
            """
        ).fetchall()
        return [
            {
                "ts":           r["bucket"],
                "tokens_in":    r["tokens_in"] or 0,
                "tokens_out":   r["tokens_out"] or 0,
                "cache_read":   r["cache_read"] or 0,
                "cache_create": r["cache_create"] or 0,
                "events":       r["events"] or 0,
                "prompts":      r["prompts"] or 0,
            }
            for r in rows
        ]

    @app.get("/api/memory-scatter")
    def api_memory_scatter():
        """Per-memory-write (bytes, retrieval_hits, class).

        Drives the effectiveness scatter.
        """
        rows = db._conn.execute(
            """
            SELECT
                mw.source_event_id AS id,
                json_extract(e.payload_json, '$.bytes') AS bytes,
                mw.retrieval_hits                        AS hits,
                r.class                                  AS roi_class,
                r.score                                  AS roi_score,
                mw.path
            FROM memory_writes mw
            JOIN events     e ON e.id = mw.source_event_id
            LEFT JOIN roi_scores r
              ON r.scope_kind = 'memory_write' AND r.scope_id = mw.source_event_id
            """
        ).fetchall()
        return [
            {
                "id":    r["id"],
                "bytes": int(r["bytes"] or 0),
                "hits":  int(r["hits"] or 0),
                "class": r["roi_class"],
                "score": r["roi_score"],
                "path":  r["path"],
            }
            for r in rows
        ]

    @app.get("/api/llm-judgments")
    def api_llm_judgments():
        """Per-prompt local-LLM verdicts with reasoning + cost context.

        Drives the dashboard's judgment list so the user can literally read
        what the local LLM thought about each prompt.
        """
        rows = db._conn.execute(
            """
            SELECT j.prompt_event_id, j.meaningful_value, j.code_quality,
                   j.output_durability, j.efficiency, j.aggregate,
                   j.reasoning, j.wasteful_patterns_json, j.model, j.judged_at,
                   a.cost_tokens, a.session_id,
                   e.payload_json,
                   r.class AS roi_class, r.score AS roi_score
            FROM llm_judgments j
            LEFT JOIN attributions a ON a.prompt_event_id = j.prompt_event_id
            LEFT JOIN events       e ON e.id             = j.prompt_event_id
            LEFT JOIN roi_scores   r ON r.scope_kind='prompt' AND r.scope_id = j.prompt_event_id
            ORDER BY j.aggregate DESC
            """
        ).fetchall()
        names = db.session_names()
        out = []
        for r in rows:
            text = json.loads(r["payload_json"] or "{}").get("text", "")
            try:
                patterns = json.loads(r["wasteful_patterns_json"] or "[]")
            except Exception:
                patterns = []
            out.append({
                "prompt_id":         r["prompt_event_id"],
                "session_id":        r["session_id"],
                "session_name":      names.get(r["session_id"], {}).get("name"),
                "text":              (text or "")[:280],
                "cost_tokens":       r["cost_tokens"] or 0,
                "meaningful_value":  r["meaningful_value"],
                "code_quality":      r["code_quality"],
                "output_durability": r["output_durability"],
                "efficiency":        r["efficiency"],
                "aggregate":         r["aggregate"],
                "reasoning":         r["reasoning"],
                "wasteful_patterns": patterns,
                "model":             r["model"],
                "judged_at":         r["judged_at"],
                "roi_class":         r["roi_class"],
                "roi_score":         r["roi_score"],
            })
        return out

    @app.get("/api/llm-summary")
    def api_llm_summary():
        """Aggregate LLM stats for the KPI row."""
        return db.llm_judgments_summary()

    # ---- manager-view endpoints ----

    @app.get("/api/team")
    def api_team():
        """Top-line team rollup for the manager landing page.

        Bosses land here and want to know instantly: how many people are
        using Claude, how much are they spending, is the spend producing
        anything, and what are the most common patterns of waste.
        """
        employees = db.employees_with_stats(registry)
        active = [e for e in employees if e["session_count"] > 0]

        total_cost = sum(e["total_cost"] for e in active)
        # ROI totals across the entire team's sessions.
        roi_totals = {"HIGH_VALUE": 0, "TRANSIENT_VALUE": 0,
                      "LOW_VALUE": 0, "WASTED": 0, "UNSCORED": 0}
        eff_values: list[float] = []
        for e in active:
            for cls, n in (e.get("roi_counts") or {}).items():
                roi_totals[cls] = roi_totals.get(cls, 0) + n
            if e.get("avg_efficiency") is not None:
                eff_values.append(e["avg_efficiency"])

        avg_eff = sum(eff_values) / len(eff_values) if eff_values else None
        high_count = roi_totals.get("HIGH_VALUE", 0)
        waste_count = (
            roi_totals.get("LOW_VALUE", 0)
            + roi_totals.get("WASTED", 0)
        )

        return {
            "active_employees":      len(active),
            "total_employees":       len(employees),
            "total_cost":            total_cost,
            "avg_efficiency":        avg_eff,
            "high_value_sessions":   high_count,
            "waste_alerts":          waste_count,
            "roi_totals":            roi_totals,
            "top_waste_patterns":    db.team_waste_patterns(limit=10),
        }

    @app.get("/api/employees")
    def api_employees():
        """Per-employee rollup powering the employee card grid."""
        return db.employees_with_stats(registry)

    @app.get("/api/employees/{employee_id}")
    def api_employee(employee_id: str):
        """Deep-dive: one employee plus their full session list."""
        emp = registry.get(employee_id)
        if emp is None:
            # Registry may not have the employee yet (edited config) —
            # still surface any sessions attributed to the id so nothing
            # vanishes silently.
            stats_row = next(
                (e for e in db.employees_with_stats(registry) if e["id"] == employee_id),
                None,
            )
            if stats_row is None:
                raise HTTPException(404, f"no such employee: {employee_id}")
            rollup = stats_row
        else:
            rollup = next(
                (e for e in db.employees_with_stats(registry) if e["id"] == emp.id),
                None,
            ) or {"id": emp.id, "name": emp.name, "role": emp.role, "team": emp.team,
                  "session_count": 0, "total_cost": 0, "file_write_bytes": 0,
                  "tool_calls": 0, "tool_successes": 0,
                  "avg_llm": None, "avg_efficiency": None, "avg_meaningful": None,
                  "roi_counts": {"HIGH_VALUE": 0, "TRANSIENT_VALUE": 0,
                                 "LOW_VALUE": 0, "WASTED": 0, "UNSCORED": 0},
                  "top_waste": [], "last_active": None}

        rollup["sessions"] = db.sessions_for_employee(rollup["id"], registry)
        return rollup

    @app.get("/api/query")
    def api_query(q: str, top_k: int = 5):
        idx = RetrievalIndex(data_dir / "retrieval")
        results = idx.query(q, top_k=top_k)
        return [
            {
                "doc_id": r.doc_id, "kind": r.kind, "score": r.score,
                "embedding_score": r.embedding_score, "keyword_score": r.keyword_score,
                "title": r.title, "snippet": r.snippet, "meta": r.meta,
            }
            for r in results
        ]

    return app
