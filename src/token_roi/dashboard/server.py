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
import logging
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
from ..pricing import format_currency


log = logging.getLogger(__name__)


def _safe_json_loads(raw: str | None, default):
    """Parse a JSON blob from the DB, returning ``default`` on corruption.

    One corrupt payload must not fail the whole API response; this keeps
    the dashboard usable when a single event row has a malformed payload.
    """
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        log.warning("corrupt JSON in DB row (%s); using default", e)
        return default


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
    # RetrievalIndex does non-trivial work on construction (loads embedding
    # weights). Build once per process rather than per /api/query request.
    retrieval = RetrievalIndex(data_dir / "retrieval")

    here = Path(__file__).resolve().parent
    templates = Jinja2Templates(directory=str(here / "templates"))

    # Cache-busting version tag for static assets. The dashboard iterates
    # the frontend heavily during development — without this, a user who
    # `token-roi dashboard`s after a code update keeps the old JS in
    # browser cache and sees stale behaviour (e.g. generic "Opus 4"
    # badges) while the backend already ships the corrected data. We
    # compute a hash of every file under static/ at startup; if any
    # changed since the last dashboard run, the <script> URLs carry a
    # new ?v=… and the browser re-fetches automatically.
    import hashlib as _hashlib
    _asset_hash = _hashlib.sha256()
    for _p in sorted((here / "static").glob("**/*")):
        if _p.is_file():
            _asset_hash.update(_p.name.encode())
            _asset_hash.update(_p.read_bytes())
    asset_version = _asset_hash.hexdigest()[:12]

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
                "asset_version": asset_version,
            },
        )

    # ---- JSON API ----

    @app.get("/api/roi/summary")
    def api_roi_summary():
        return db.roi_summary()

    @app.get("/api/sessions")
    def api_sessions():
        names = db.session_names()
        cost_map = db.session_cost_map()
        out = []
        for sid in db.all_sessions():
            t = db.session_totals(sid)
            if t is None:
                continue
            row = db.get_roi_score("session", sid)
            name_info = names.get(sid, {})
            cost_usd = cost_map.get(sid, 0.0)
            out.append({
                "session_id":    sid,
                "name":          name_info.get("name"),
                "summary":       name_info.get("summary"),
                "event_count":   t.event_count,
                "prompts":       t.prompt_count,
                "tools":         t.tool_call_count,
                "tokens_in":     t.tokens_in,
                "tokens_out":    t.tokens_out,
                "cached_tokens": t.cached_tokens,
                "memory_writes": t.memory_writes,
                "retrievals":    t.retrievals,
                "total_tokens":  t.total_tokens,
                # Per-model priced USD — matches the Overview KPI and the
                # Projects-tab cards. Added alongside the legacy token
                # field so older clients don't break.
                "cost_usd":       cost_usd,
                "formatted_cost": format_currency(cost_usd),
                "roi_class":     row["class"] if row else None,
                "roi_score":     row["score"] if row else None,
            })
        # Rank by USD (boss-view default) rather than raw token count.
        out.sort(key=lambda r: r["cost_usd"], reverse=True)
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
            session_derivation = _safe_json_loads(session_roi["derivation_json"], None)

        # Aggregate per-prompt token usage from the assistant_message events
        # listed in attributions.cost_event_ids_json. Tokens live on the
        # downstream assistant turn(s), not on the user_prompt event itself,
        # so we sum across the JSON array of contributing event ids.
        prompts = db._conn.execute(
            """
            WITH prompt_tokens AS (
                SELECT
                    a.prompt_event_id,
                    SUM(COALESCE(e2.tokens_in, 0))             AS tokens_in,
                    SUM(COALESCE(e2.tokens_out, 0))            AS tokens_out,
                    SUM(COALESCE(e2.cached_tokens, 0))         AS cached_tokens,
                    SUM(COALESCE(e2.cache_creation_tokens, 0)) AS cache_creation_tokens,
                    MAX(e2.model)                              AS prompt_model
                FROM attributions a, json_each(a.cost_event_ids_json) je
                LEFT JOIN events e2 ON e2.id = je.value
                WHERE a.session_id = ?
                GROUP BY a.prompt_event_id
            )
            SELECT a.prompt_event_id, a.cost_tokens, a.durable_bytes,
                   a.file_write_bytes, a.tool_calls, a.tool_successes,
                   a.retrieval_count, a.outcome_score, a.reuse_score,
                   e.payload_json, e.ts,
                   pt.tokens_in, pt.tokens_out, pt.cached_tokens,
                   pt.cache_creation_tokens, pt.prompt_model,
                   r.class, r.score, r.derivation_json,
                   j.meaningful_value, j.code_quality, j.output_durability,
                   j.efficiency, j.aggregate, j.reasoning, j.wasteful_patterns_json,
                   j.model AS judge_model
            FROM attributions a
            LEFT JOIN events         e  ON e.id = a.prompt_event_id
            LEFT JOIN prompt_tokens  pt ON pt.prompt_event_id = a.prompt_event_id
            LEFT JOIN roi_scores     r  ON r.scope_kind = 'prompt' AND r.scope_id = a.prompt_event_id
            LEFT JOIN llm_judgments  j  ON j.prompt_event_id = a.prompt_event_id
            WHERE a.session_id = ?
            ORDER BY a.cost_tokens DESC
            """,
            (session_id, session_id),
        ).fetchall()

        # Cap full prompt text at 50KB so a pathological paste (e.g. a
        # giant code dump) can't bloat the modal payload past usable size.
        TEXT_CAP = 50_000

        prompt_items = []
        for r in prompts:
            payload = _safe_json_loads(r["payload_json"], {})
            text = payload.get("text", "") if isinstance(payload, dict) else ""
            full_len = len(text)
            prompt_items.append({
                "id":                    r["prompt_event_id"],
                "text":                  text[:TEXT_CAP],
                "text_full_length":      full_len,
                "text_truncated":        full_len > TEXT_CAP,
                "ts":                    r["ts"],
                "cost_tokens":           r["cost_tokens"],
                "tokens_in":             int(r["tokens_in"] or 0),
                "tokens_out":            int(r["tokens_out"] or 0),
                "cached_tokens":         int(r["cached_tokens"] or 0),
                "cache_creation_tokens": int(r["cache_creation_tokens"] or 0),
                "model":                 r["prompt_model"],
                "durable_bytes":         r["durable_bytes"],
                "file_write_bytes":      r["file_write_bytes"] or 0,
                "tool_calls":            r["tool_calls"] or 0,
                "tool_successes":        r["tool_successes"] or 0,
                "retrieval_count":       r["retrieval_count"],
                "outcome_score":         r["outcome_score"],
                "reuse_score":           r["reuse_score"],
                "class":                 r["class"],
                "score":                 r["score"],
                "llm": ({
                    "meaningful_value":  r["meaningful_value"],
                    "code_quality":      r["code_quality"],
                    "output_durability": r["output_durability"],
                    "efficiency":        r["efficiency"],
                    "aggregate":         r["aggregate"],
                    "reasoning":         r["reasoning"],
                    "wasteful_patterns": _safe_json_loads(r["wasteful_patterns_json"], []),
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

        # Per-model cost split for this session. Answers "which model
        # burned the tokens?" — a session that ran 90% Opus costs 5x
        # what the same work on Sonnet would have. We also expose the
        # per-category USD math (tokens × rate = subtotal) so the modal
        # can reveal *how* the dollar figure was arrived at — otherwise
        # "$143.01" is opaque and the boss can't sanity-check it.
        from ..pricing import lookup_pricing
        models = db.model_breakdown(session_ids=[session_id])
        for m in models:
            m["formatted_cost"] = format_currency(m["cost_usd"])
            p = lookup_pricing(m["model"])
            m["pricing"] = {
                "input_per_m":      p.input_per_m,
                "output_per_m":     p.output_per_m,
                "cache_read_per_m": p.cache_read_per_m,
                "cache_write_per_m": p.cache_write_per_m,
            }
            m["cost_components"] = {
                "input_usd":        m["tokens_in"]    * p.input_per_m       / 1_000_000,
                "output_usd":       m["tokens_out"]   * p.output_per_m      / 1_000_000,
                "cache_read_usd":   m["cache_read"]   * p.cache_read_per_m  / 1_000_000,
                "cache_create_usd": m["cache_create"] * p.cache_write_per_m / 1_000_000,
            }
        session_cost_usd = sum(m["cost_usd"] for m in models)

        return {
            "session_id": session_id,
            "name":       summary_info.get("name"),
            "summary":    summary_info.get("summary"),
            "roi_class":  session_roi["class"] if session_roi else None,
            "roi_score":  session_roi["score"] if session_roi else None,
            "roi_derivation": session_derivation,
            "cost_usd":   session_cost_usd,
            "formatted_cost": format_currency(session_cost_usd),
            "model_breakdown": models,
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
        """Biggest-spending prompts with USD cost alongside tokens.

        The boss KPI is money, not tokens — we attach the per-model
        priced USD cost so each row in the leaderboard reads as a
        dollar amount first.
        """
        rows = db.run_view("top_spenders.sql")
        names = db.session_names()
        cost_map = db.session_cost_map()
        # Token→USD conversion at the prompt level uses the prompt's
        # share of its session's cost, weighted by cost_tokens. This
        # avoids running a separate per-prompt per-model aggregate
        # query for every leaderboard render.
        session_token_totals: dict[str, int] = {}
        for r in rows:
            session_token_totals[r["session_id"]] = (
                session_token_totals.get(r["session_id"], 0) + (r["cost_tokens"] or 0)
            )
        out = []
        for r in rows:
            sid = r["session_id"]
            sess_total = session_token_totals.get(sid, 0) or 1
            share = (r["cost_tokens"] or 0) / sess_total
            cost_usd = cost_map.get(sid, 0.0) * share
            out.append({
                "prompt_id": r["prompt_id"],
                "session_id": sid,
                "session_name": names.get(sid, {}).get("name"),
                "text": (_safe_json_loads(r["prompt_payload"], {}) or {}).get("text", "")[:280],
                "cost_tokens": r["cost_tokens"],
                "cost_usd": cost_usd,
                "formatted_cost": format_currency(cost_usd),
                "class": r["class"],
                "score": r["score"],
            })
        return out

    @app.get("/api/black-holes")
    def api_black_holes():
        """Sessions classified LOW_VALUE or WASTED, with their USD bill.

        USD cost is computed per-model by ``AnalyticsDB.session_cost_map``,
        so a session that mixes Opus and Sonnet is priced correctly rather
        than blended against a single rate.
        """
        rows = db.run_view("black_holes.sql")
        names = db.session_names()
        cost_map = db.session_cost_map()
        out = []
        for r in rows:
            d = dict(r)
            cost = cost_map.get(d.get("session_id"), 0.0)
            d["cost_usd"] = cost
            d["formatted_cost"] = format_currency(cost)
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
                "bytes": (_safe_json_loads(r["payload_json"], {}) or {}).get("bytes", 0),
            }
            for r in rows
        ]

    @app.get("/api/kpis")
    def api_kpis():
        """Hero-row KPIs incl. per-model USD cost and audit-gap counter."""
        k = db.kpis()
        k["formatted_cost"] = format_currency(k["total_cost_usd"])
        # Surface malformed-event count so audit-trail gaps are visible
        # rather than buried in stderr.
        k["malformed_events_skipped"] = store.malformed_events_skipped
        return k

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
            payload = _safe_json_loads(r["payload_json"], {}) or {}
            text = payload.get("text", "") if isinstance(payload, dict) else ""
            patterns = _safe_json_loads(r["wasteful_patterns_json"], [])
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
        using Claude, how much are they spending (in dollars, not
        tokens — that's the actual business question), is the spend
        producing anything, and what are the most common patterns of
        waste. We compute USD per-model so a mixed Opus/Sonnet team is
        priced correctly.
        """
        employees = db.employees_with_stats(registry)
        active = [e for e in employees if e["session_count"] > 0]

        total_cost_tokens = sum(e["total_cost"] for e in active)
        total_cost_usd = db.total_cost()

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

        # Durable-output stat for the "cost per KB shipped" line.
        file_bytes_row = db._conn.execute(
            """SELECT COALESCE(SUM(json_extract(payload_json, '$.bytes')), 0) AS b
                 FROM events WHERE type = 'file_write'"""
        ).fetchone()
        total_file_bytes = int(file_bytes_row["b"] or 0)
        total_kb = total_file_bytes / 1024.0
        cost_per_kb = (total_cost_usd / total_kb) if total_kb > 0 else None

        return {
            "active_employees":      len(active),
            "total_employees":       len(employees),
            "total_cost":            total_cost_tokens,       # tokens (legacy)
            "total_cost_usd":        total_cost_usd,
            "formatted_cost":        format_currency(total_cost_usd),
            "total_file_bytes":      total_file_bytes,
            "cost_per_kb":           cost_per_kb,
            "formatted_cost_per_kb": (
                format_currency(cost_per_kb) if cost_per_kb is not None else None
            ),
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
        # Compute once — this query is expensive (multi-way join).
        all_stats = db.employees_with_stats(registry)
        emp = registry.get(employee_id)
        if emp is None:
            # Registry may not have the employee yet (edited config) —
            # still surface any sessions attributed to the id so nothing
            # vanishes silently.
            stats_row = next(
                (e for e in all_stats if e["id"] == employee_id),
                None,
            )
            if stats_row is None:
                raise HTTPException(404, f"no such employee: {employee_id}")
            rollup = stats_row
        else:
            rollup = next(
                (e for e in all_stats if e["id"] == emp.id),
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

    # Bounds on retrieval top_k. A value this small still covers every
    # realistic UI and prevents ?top_k=1000000 from exhausting memory.
    _MAX_TOP_K = 50

    @app.get("/api/query")
    def api_query(q: str, top_k: int = 5):
        if not q or not q.strip():
            raise HTTPException(400, "query string 'q' is required")
        top_k = max(1, min(_MAX_TOP_K, top_k))
        results = retrieval.query(q, top_k=top_k)
        return [
            {
                "doc_id": r.doc_id, "kind": r.kind, "score": r.score,
                "embedding_score": r.embedding_score, "keyword_score": r.keyword_score,
                "title": r.title, "snippet": r.snippet, "meta": r.meta,
            }
            for r in results
        ]

    # ---- projects ----

    @app.get("/api/projects")
    def api_projects():
        """Per-project rollup for the boss dashboard.

        Each project corresponds to one Claude Code workspace. Returns
        USD cost (per-model priced), session count, durable bytes
        produced, ROI class distribution, model mix, and a cost-per-KB
        productivity ratio so the boss can see "this project spent $X
        on Opus vs Sonnet and produced Y KB of code" at a glance.
        """
        rows = db.projects_with_stats()
        for r in rows:
            r["formatted_cost"] = format_currency(r["cost_usd"])
            if r.get("cost_per_kb") is not None:
                r["formatted_cost_per_kb"] = format_currency(r["cost_per_kb"])
            else:
                r["formatted_cost_per_kb"] = None
            # Per-model split so the card can show "Opus 85% · Sonnet 15%".
            models = db.model_breakdown(project_slug=r["slug"])
            for m in models:
                m["formatted_cost"] = format_currency(m["cost_usd"])
            r["model_breakdown"] = models
        return rows

    @app.get("/api/projects/{slug}")
    def api_project(slug: str):
        """Drill-down for one project: session list, cost, ROI mix, model mix."""
        rollup = next(
            (p for p in db.projects_with_stats() if p["slug"] == slug),
            None,
        )
        if rollup is None:
            raise HTTPException(404, f"no such project: {slug}")
        rollup["formatted_cost"] = format_currency(rollup["cost_usd"])
        rollup["formatted_cost_per_kb"] = (
            format_currency(rollup["cost_per_kb"])
            if rollup.get("cost_per_kb") is not None else None
        )
        sessions = db.sessions_for_project(slug)
        for s in sessions:
            s["formatted_cost"] = format_currency(s["cost_usd"])
        rollup["sessions"] = sessions
        models = db.model_breakdown(project_slug=slug)
        for m in models:
            m["formatted_cost"] = format_currency(m["cost_usd"])
        rollup["model_breakdown"] = models
        return rollup

    @app.get("/api/model-breakdown")
    def api_model_breakdown():
        """Workspace-wide per-model cost split for the overview KPI row."""
        models = db.model_breakdown()
        for m in models:
            m["formatted_cost"] = format_currency(m["cost_usd"])
        return models

    return app
