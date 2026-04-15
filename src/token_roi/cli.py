"""Command-line interface.

This is the main user-facing surface. Every operation — init, ingest, score,
compress, query, replay, dashboard — is available as a subcommand here.

Design notes:
    - All commands accept `--data-dir` (default: ./data). The whole skill
      is rooted at that directory.
    - Text output is intentionally Unix-style: one thing per line, stable
      columns, easy to pipe through `| awk` or `| jq`.
    - Every command that mutates writes exactly one event or row-set and
      exits. No long-running state here except `dashboard`.
    - Non-zero exit codes are reserved for real failures (missing data,
      bad args), not for "found nothing" — empty results exit 0.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Iterable

from .attribution import AttributionGraph
from .compression import CompressionEngine
from .db import AnalyticsDB
from .employees import EmployeeRegistry, format_employee_table
from .events import EventType
from .i18n import SUPPORTED_LOCALES, set_locale
from .memory import MemoryLayer
from .replay import ReplayOptions, Replayer
from .retrieval import (
    IndexedDoc,
    RetrievalIndex,
    build_docs_from_events,
    build_docs_from_memory,
)
from .roi import ROIClassifier
from .storage import EventStore
from .telemetry import Telemetry


def _default_data_dir() -> Path:
    # The `data/` alongside the skill is the canonical local location.
    # Users can override with --data-dir or the TOKEN_ROI_DATA_DIR env var.
    env = os.environ.get("TOKEN_ROI_DATA_DIR")
    if env:
        return Path(env)
    root = Path(__file__).resolve().parents[2]
    return root / "data"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="token-roi",
        description="Local-first flight recorder + ROI analyzer for agentic sessions.",
    )
    p.add_argument("--data-dir", type=Path, default=None,
                   help="Root data directory (default: ./data next to the skill).")
    p.add_argument("--otel", action="store_true",
                   help="Initialize OpenTelemetry export (requires opentelemetry-sdk).")
    # Locale controls two things at once: the LLM system prompt used by
    # `judge` / `name-sessions`, and the dashboard UI language. Applies
    # process-wide via set_locale() in main().
    p.add_argument("--locale", type=str, default=None,
                   choices=list(SUPPORTED_LOCALES),
                   help="Locale for LLM prompts and dashboard UI (overrides TOKEN_ROI_LOCALE).")
    sub = p.add_subparsers(dest="cmd", required=True)

    # init
    s = sub.add_parser("init", help="Initialize data dir, DB, and stub MEMORY.md.")
    s.set_defaults(func=cmd_init)

    # ingest
    s = sub.add_parser("ingest",
                       help="Rebuild the SQLite index from raw_events/.")
    s.add_argument("--since", type=str, default=None,
                   help="Only re-ingest events newer than this timestamp or NdHdM window.")
    s.set_defaults(func=cmd_ingest)

    # install-hooks
    s = sub.add_parser("install-hooks",
                       help="Register hook scripts into Claude Code settings.json.")
    s.add_argument("--settings", type=Path, default=Path("~/.claude/settings.json").expanduser())
    s.add_argument("--python", type=str, default=sys.executable)
    s.set_defaults(func=cmd_install_hooks)

    # capture
    s = sub.add_parser("capture",
                       help="Capture a prompt/response pair from stdin (for non-hooked harnesses).")
    s.add_argument("--session-id", type=str, default=None)
    s.add_argument("--role", choices=["user", "assistant"], required=True)
    s.add_argument("--tokens-in", type=int, default=0)
    s.add_argument("--tokens-out", type=int, default=0)
    s.add_argument("--model", type=str, default=None)
    s.set_defaults(func=cmd_capture)

    # score
    s = sub.add_parser("score", help="Run attribution + ROI scoring.")
    s.add_argument("--session", type=str, default=None)
    s.add_argument("--since", type=str, default=None)
    s.add_argument("--all", action="store_true")
    s.set_defaults(func=cmd_score)

    # explain
    s = sub.add_parser("explain",
                       help="Print the full derivation for a given ROI score.")
    s.add_argument("--kind", choices=["prompt", "session", "memory_write"], default="prompt")
    s.add_argument("--id", required=True)
    s.set_defaults(func=cmd_explain)

    # attribute
    s = sub.add_parser("attribute",
                       help="Print the attribution for a single prompt.")
    s.add_argument("--event", required=True, help="user_prompt event id")
    s.set_defaults(func=cmd_attribute)

    # compress
    s = sub.add_parser("compress",
                       help="Run a compression pass over memory.")
    s.add_argument("--max-topics", type=int, default=40)
    s.add_argument("--since", type=str, default=None)
    s.add_argument("--session-id", type=str, default=None,
                   help="Attach compression events to this session id.")
    s.set_defaults(func=cmd_compress)

    # index
    s = sub.add_parser("index-memory",
                       help="Build / refresh the retrieval index.")
    s.add_argument("--include-events", action="store_true",
                   help="Also index raw prompts + assistant messages.")
    s.set_defaults(func=cmd_index_memory)

    # query
    s = sub.add_parser("query", help="Hybrid retrieval over memory + events.")
    s.add_argument("text", nargs="+")
    s.add_argument("--top-k", type=int, default=5)
    s.add_argument("--session-id", type=str, default=None)
    s.add_argument("--json", action="store_true", help="Emit JSON output.")
    s.set_defaults(func=cmd_query)

    # replay
    s = sub.add_parser("replay", help="Deterministic session replay from raw_events.")
    s.add_argument("--session", required=True)
    s.add_argument("--from", dest="from_event", default=None)
    s.add_argument("--mode", choices=["text", "jsonl"], default="text")
    s.add_argument("--show-payload", action="store_true")
    s.set_defaults(func=cmd_replay)

    # list sessions / summary
    s = sub.add_parser("sessions", help="List sessions with totals.")
    s.set_defaults(func=cmd_sessions)

    # roi-summary
    s = sub.add_parser("roi-summary",
                       help="Aggregate ROI classifications across all scopes.")
    s.set_defaults(func=cmd_roi_summary)

    # top spenders / black holes / orphans
    s = sub.add_parser("view", help="Run a materialized view.")
    s.add_argument("name", choices=["top_spenders.sql", "orphan_memory.sql", "black_holes.sql"])
    s.set_defaults(func=cmd_view)

    # employees
    s = sub.add_parser(
        "employees",
        help="Manage the employee registry (data/employees.json).",
    )
    emp_sub = s.add_subparsers(dest="employees_cmd", required=True)
    emp_sub.add_parser("list", help="List employees and their rollup stats.")
    show = emp_sub.add_parser("show", help="Show detail for one employee.")
    show.add_argument("employee_id", help="Employee id (e.g. chinaharry).")
    s.set_defaults(func=cmd_employees)

    # name-sessions
    s = sub.add_parser(
        "name-sessions",
        help="Use the local LLM to give each session a short human-readable name.",
    )
    s.add_argument("--endpoint", type=str, default="http://localhost:1234/v1")
    s.add_argument("--model", type=str, default=None)
    s.add_argument("--force", action="store_true",
                   help="Re-name sessions that already have a cached name.")
    s.add_argument("--timeout", type=int, default=120)
    s.set_defaults(func=cmd_name_sessions)

    # judge
    s = sub.add_parser(
        "judge",
        help="Run a local LLM (LM Studio / Ollama) to judge prompt value.",
    )
    s.add_argument("--endpoint", type=str, default="http://localhost:1234/v1",
                   help="OpenAI-compatible endpoint (default: LM Studio).")
    s.add_argument("--model", type=str, default=None,
                   help="Model id. Defaults to the first model the server reports.")
    s.add_argument("--session", type=str, default=None,
                   help="Limit to prompts in this session.")
    s.add_argument("--since", type=str, default=None,
                   help="Only judge prompts newer than this window (e.g. 7d).")
    s.add_argument("--limit", type=int, default=None,
                   help="Stop after this many judgments (useful for smoke tests).")
    s.add_argument("--force", action="store_true",
                   help="Re-judge prompts that already have a cached judgment.")
    s.add_argument("--list-models", action="store_true",
                   help="List models available at the endpoint and exit.")
    s.add_argument("--timeout", type=int, default=120,
                   help="Per-call HTTP timeout in seconds.")
    s.set_defaults(func=cmd_judge)

    # import
    s = sub.add_parser(
        "import",
        help="Import session history from an external source (Claude Code, ...).",
    )
    s.add_argument("source", choices=["claude-code"],
                   help="Which external log format to import.")
    s.add_argument("--from", dest="from_path", type=Path,
                   default=Path("~/.claude/projects").expanduser(),
                   help="Path to scan (file, project dir, or projects root).")
    s.add_argument("--project", type=str, default=None,
                   help="Filter by project slug substring (e.g. 'Bossify').")
    s.set_defaults(func=cmd_import)

    # dashboard
    s = sub.add_parser("dashboard", help="Launch the local FastAPI dashboard.")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8787)
    s.set_defaults(func=cmd_dashboard)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    data_dir = (args.data_dir or _default_data_dir()).resolve()
    args.data_dir = data_dir
    # --locale overrides env (TOKEN_ROI_LOCALE) if present. Applied
    # process-wide so any library-level default reads the right language.
    if getattr(args, "locale", None):
        set_locale(args.locale)
    if args.otel:
        Telemetry.init()
    try:
        return int(args.func(args) or 0)
    except Exception as e:  # noqa: BLE001
        # Log full traceback in a conservative way — useful for debugging.
        import traceback
        print(f"error: {e}", file=sys.stderr)
        if os.environ.get("TOKEN_ROI_DEBUG") == "1":
            traceback.print_exc()
        return 2


# ---- commands ----

def cmd_init(args) -> int:
    data_dir = args.data_dir
    (data_dir / "raw_events").mkdir(parents=True, exist_ok=True)
    (data_dir / "snapshots").mkdir(parents=True, exist_ok=True)
    (data_dir / "memory" / "topics").mkdir(parents=True, exist_ok=True)
    (data_dir / "retrieval" / "embeddings").mkdir(parents=True, exist_ok=True)
    (data_dir / "retrieval" / "indexes").mkdir(parents=True, exist_ok=True)
    (data_dir / "analytics").mkdir(parents=True, exist_ok=True)
    (data_dir / "dashboard").mkdir(parents=True, exist_ok=True)

    db = AnalyticsDB(data_dir / "analytics" / "roi.db")
    db.migrate()

    memory_index = data_dir / "memory" / "MEMORY.md"
    if not memory_index.exists():
        memory_index.write_text("", encoding="utf-8")

    print(f"initialized {data_dir}")
    return 0


def cmd_ingest(args) -> int:
    store = EventStore(args.data_dir)
    db = AnalyticsDB(args.data_dir / "analytics" / "roi.db")
    db.migrate()
    since_ts = _parse_since(args.since)
    events = store.iter_all_sessions(since_ts=since_ts)
    count = db.rebuild_from(events)
    print(f"indexed {count} events")
    return 0


def cmd_install_hooks(args) -> int:
    from .hooks import install_into_settings
    # The hooks/ directory is sibling to src/token_roi — resolve both.
    skill_root = Path(__file__).resolve().parents[2]
    hooks_dir = skill_root / "hooks"
    if not hooks_dir.exists():
        print(f"no hooks dir at {hooks_dir}", file=sys.stderr)
        return 2
    install_into_settings(
        args.settings,
        hooks_dir=hooks_dir,
        data_dir=args.data_dir,
        python=args.python,
    )
    print(f"installed hooks into {args.settings}")
    return 0


def cmd_capture(args) -> int:
    store = EventStore(args.data_dir)
    sid = store.start_session(args.session_id)
    text = sys.stdin.read()
    if args.role == "user":
        ev = store.append_user_prompt(sid, text)
    else:
        ev = store.append_assistant_message(
            sid, text,
            tokens_in=args.tokens_in, tokens_out=args.tokens_out,
            model=args.model,
        )
    print(f"{ev.id}\tsid={sid}\ttype={ev.type.value}\tseq={ev.seq}")
    return 0


def cmd_score(args) -> int:
    store = EventStore(args.data_dir)
    db = AnalyticsDB(args.data_dir / "analytics" / "roi.db")
    db.migrate()
    # Always re-ingest so new events captured since the last score run
    # are picked up. This is O(events); cheap for the local-first case.
    # The rebuild is atomic: on failure the previous DB state is unchanged.
    if store.list_sessions():
        db.rebuild_from(store.iter_all_sessions())

    graph = AttributionGraph(db)
    classifier = ROIClassifier(db)

    if args.session:
        sessions: Iterable[str] = [args.session]
    elif args.since:
        sessions = db.sessions_since(_parse_since(args.since) or 0)
    else:
        sessions = db.all_sessions()
    sessions = list(sessions)

    total_prompts = graph.attribute_all(sessions)
    prompts_scored = classifier.score_all_prompts()
    sessions_scored = classifier.score_all_sessions()
    memory_scored = classifier.score_all_memory_writes()

    summary = db.roi_summary()

    print(f"sessions={len(sessions)} prompt_attributions={total_prompts} "
          f"prompts_scored={prompts_scored} sessions_scored={sessions_scored} "
          f"memory_writes_scored={memory_scored}")
    for cls, n in sorted(summary.items()):
        print(f"  {cls:<18} {n}")
    return 0


def cmd_explain(args) -> int:
    db = AnalyticsDB(args.data_dir / "analytics" / "roi.db")
    row = db.get_roi_score(args.kind, args.id)
    if row is None:
        print(f"no score for {args.kind}/{args.id}", file=sys.stderr)
        return 1
    print(f"class:   {row['class']}")
    print(f"score:   {row['score']:.3f}")
    print(f"computed_at: {row['computed_at']}")
    deriv = json.loads(row["derivation_json"])
    print("derivation:")
    print(json.dumps(deriv, indent=2))
    return 0


def cmd_attribute(args) -> int:
    db = AnalyticsDB(args.data_dir / "analytics" / "roi.db")
    row = db._conn.execute(
        """SELECT * FROM attributions WHERE prompt_event_id = ?""", (args.event,)
    ).fetchone()
    if row is None:
        print(f"no attribution for {args.event}", file=sys.stderr)
        return 1
    for k in row.keys():
        print(f"{k:>16}: {row[k]}")
    return 0


def cmd_compress(args) -> int:
    db = AnalyticsDB(args.data_dir / "analytics" / "roi.db")
    db.migrate()
    store = EventStore(args.data_dir)
    memory = MemoryLayer(args.data_dir / "memory", store=store)
    session_id = args.session_id
    if session_id is None:
        session_id = store.start_session()
    engine = CompressionEngine(db, memory, session_id_for_log=session_id)
    summary = engine.run(
        since_ts=_parse_since(args.since),
        max_topics=args.max_topics,
    )
    print(json.dumps(summary, indent=2, default=str))
    return 0


def cmd_index_memory(args) -> int:
    db = AnalyticsDB(args.data_dir / "analytics" / "roi.db")
    memory = MemoryLayer(args.data_dir / "memory")
    idx = RetrievalIndex(args.data_dir / "retrieval")
    docs = build_docs_from_memory(memory)
    if args.include_events:
        docs += build_docs_from_events(db)
    n = idx.ingest(docs, replace=True)
    print(f"indexed {n} docs using backend={idx.backend.name}")
    return 0


def cmd_query(args) -> int:
    store = EventStore(args.data_dir)
    idx = RetrievalIndex(args.data_dir / "retrieval", store=store)
    q = " ".join(args.text)
    sid = args.session_id or store.start_session()
    results = idx.query(q, top_k=args.top_k, session_id=sid)
    if args.json:
        print(json.dumps(
            [{
                "doc_id": r.doc_id, "kind": r.kind, "score": r.score,
                "embedding_score": r.embedding_score,
                "keyword_score": r.keyword_score,
                "title": r.title, "snippet": r.snippet, "meta": r.meta,
            } for r in results],
            indent=2, default=str,
        ))
        return 0
    for r in results:
        print(f"{r.score:6.3f}  {r.kind:<6} {r.title}")
        print(f"        {r.snippet}")
    return 0


def cmd_replay(args) -> int:
    store = EventStore(args.data_dir)
    replayer = Replayer(store)
    opts = ReplayOptions(
        mode=args.mode,
        start_event=args.from_event,
        show_payload=args.show_payload,
    )
    n = replayer.replay_session(args.session, opts=opts)
    if n == 0 and args.mode == "text":
        print(f"no events for session {args.session}", file=sys.stderr)
        return 1
    return 0


def cmd_sessions(args) -> int:
    db = AnalyticsDB(args.data_dir / "analytics" / "roi.db")
    db.migrate()
    ids = db.all_sessions()
    if not ids:
        print("(no sessions)")
        return 0
    # UUIDs are 36 chars; truncate for readability. Full ids still live
    # in the DB and dashboard. The human-readable name comes from
    # session_summaries if the user has run `token-roi name-sessions`.
    names = db.session_names()
    header = f"{'SESSION':<14} {'NAME':<38} {'EVENTS':>7} {'PROMPTS':>8} {'TOOLS':>6} {'TOKENS_OUT':>11} {'CACHE_R':>10}"
    print(header)
    for sid in ids:
        t = db.session_totals(sid)
        if t is None:
            continue
        short = sid[:12] if len(sid) > 12 else sid
        name_info = names.get(sid, {})
        name = name_info.get("name", "")[:36]
        print(
            f"{short:<14} {name:<38} {t.event_count:>7} {t.prompt_count:>8} "
            f"{t.tool_call_count:>6} {t.tokens_out:>11} {t.cached_tokens:>10}"
        )
    return 0


def cmd_roi_summary(args) -> int:
    db = AnalyticsDB(args.data_dir / "analytics" / "roi.db")
    db.migrate()
    summary = db.roi_summary()
    if not summary:
        print("(no scores yet — run `token-roi score`)")
        return 0
    print(f"{'CLASS':<20} {'COUNT':>6}")
    for cls in ("HIGH_VALUE", "TRANSIENT_VALUE", "LOW_VALUE", "WASTED"):
        print(f"{cls:<20} {summary.get(cls, 0):>6}")
    return 0


def cmd_view(args) -> int:
    db = AnalyticsDB(args.data_dir / "analytics" / "roi.db")
    rows = db.run_view(args.name)
    if not rows:
        print("(empty)")
        return 0
    cols = rows[0].keys()
    print("\t".join(cols))
    for r in rows:
        print("\t".join(str(r[c]) for c in cols))
    return 0


def cmd_employees(args) -> int:
    """Manage the employee registry from the command line."""
    registry = EmployeeRegistry(args.data_dir)
    db = AnalyticsDB(args.data_dir / "analytics" / "roi.db")
    db.migrate()

    if args.employees_cmd == "list":
        print(f"config: {registry.config_path} "
              f"{'(default synthesized)' if not registry.config_path.exists() else ''}")
        print()
        print(format_employee_table(registry.all()))
        print()
        stats = db.employees_with_stats(registry)
        if stats:
            print(f"{'ID':<16} {'NAME':<20} {'SESSIONS':>8} {'COST':>14} {'AVG_EFF':>8}")
            print("-" * 72)
            for e in stats:
                avg_eff = f"{e['avg_efficiency']:.2f}" if e["avg_efficiency"] is not None else "—"
                print(f"{e['id']:<16} {e['name']:<20} {e['session_count']:>8} "
                      f"{e['total_cost']:>14,} {avg_eff:>8}")
        return 0

    if args.employees_cmd == "show":
        emp = registry.get(args.employee_id)
        if emp is None:
            print(f"no such employee: {args.employee_id}", file=sys.stderr)
            return 2
        print(f"{emp.name} ({emp.id})  role={emp.role or '—'}  team={emp.team or '—'}")
        print()
        sessions = db.sessions_for_employee(emp.id, registry)
        if not sessions:
            print("(no sessions)")
            return 0
        print(f"{'SESSION':<14} {'CLASS':<16} {'COST':>12} {'NAME':<36}")
        print("-" * 80)
        for s in sessions:
            print(f"{s['session_id'][:12]:<14} "
                  f"{(s['roi_class'] or 'UNSCORED'):<16} "
                  f"{s['cost']:>12,} "
                  f"{(s['name'] or '')[:34]:<36}")
        return 0

    raise ValueError(f"unknown employees subcommand: {args.employees_cmd}")


def cmd_name_sessions(args) -> int:
    """Generate LLM-backed names for sessions that don't have one yet."""
    from .llm_judge import Judge, LocalLLM
    db = AnalyticsDB(args.data_dir / "analytics" / "roi.db")
    db.migrate()
    llm = LocalLLM(endpoint=args.endpoint, model=args.model, timeout_s=args.timeout)
    if not llm.health():
        print(f"LLM endpoint not reachable at {args.endpoint}. "
              "Is LM Studio / Ollama running?", file=sys.stderr)
        return 2
    judge = Judge(db, llm)
    n = 0
    for _ in judge.summarize_all(force=args.force, progress=True):
        n += 1
    total = len(db.session_names())
    print(f"\nnamed {n} new session(s); {total} total named.")
    return 0


def cmd_judge(args) -> int:
    """Run local-LLM value judgments over prompts."""
    from .llm_judge import Judge, LocalLLM
    db = AnalyticsDB(args.data_dir / "analytics" / "roi.db")
    db.migrate()
    llm = LocalLLM(endpoint=args.endpoint, model=args.model, timeout_s=args.timeout)

    if args.list_models:
        try:
            models = llm.list_models()
        except Exception as e:
            print(f"failed to reach {args.endpoint}: {e}", file=sys.stderr)
            return 2
        for m in models:
            print(m)
        return 0

    if not llm.health():
        print(f"LLM endpoint not reachable at {args.endpoint}. "
              "Is LM Studio / Ollama running?", file=sys.stderr)
        return 2

    judge = Judge(db, llm)
    n = 0
    for j in judge.judge_all(
        session_id=args.session,
        since_ts=_parse_since(args.since),
        limit=args.limit,
        force=args.force,
        progress=True,
    ):
        n += 1
    summary = db.llm_judgments_summary()
    print()
    print(f"judged {n} new prompt(s); total cached judgments: {summary['count']}")
    if summary["count"]:
        print(f"  avg meaningful:  {summary['avg_meaningful']:.2f}")
        print(f"  avg durability:  {summary['avg_durability']:.2f}")
        print(f"  avg efficiency:  {summary['avg_efficiency']:.2f}")
        print(f"  avg aggregate:   {summary['avg_aggregate']:.2f}")
    print()
    print("Run `token-roi score` to fold these into ROI classifications.")
    return 0


def cmd_import(args) -> int:
    store = EventStore(args.data_dir)
    db = AnalyticsDB(args.data_dir / "analytics" / "roi.db")
    db.migrate()
    registry = EmployeeRegistry(args.data_dir)
    if args.source == "claude-code":
        from .importers.claude_code import ClaudeCodeImporter
        imp = ClaudeCodeImporter(store, db=db, employees=registry)
        stats = imp.import_path(args.from_path, project_filter=args.project)
        print(json.dumps(stats.to_dict(), indent=2))
        # Rebuild the index so subsequent commands see the imported events.
        db.rebuild_from(store.iter_all_sessions())
        print(f"indexed {len(store.list_sessions())} session(s) after import.")
        # Per-employee rollup gives the manager an immediate sense of how
        # the new sessions got attributed.
        employees = db.employees_with_stats(registry)
        if employees:
            print()
            print("per-employee rollup:")
            for e in employees:
                print(f"  {e['name']:<20} sessions={e['session_count']:>3} "
                      f"cost={e['total_cost']:>12,}")
        return 0
    raise ValueError(f"unknown source: {args.source}")


def cmd_dashboard(args) -> int:
    try:
        import uvicorn  # type: ignore
    except ImportError:
        print("fastapi + uvicorn required: pip install 'bossify-with-claude[dashboard]'",
              file=sys.stderr)
        return 2
    # Lazy import to keep --help fast.
    from .dashboard.server import make_app
    app = make_app(args.data_dir)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


# ---- helpers ----

def _parse_since(spec: str | None) -> float | None:
    if spec is None:
        return None
    spec = spec.strip()
    if spec.endswith("d"):
        return time.time() - int(spec[:-1]) * 86400
    if spec.endswith("h"):
        return time.time() - int(spec[:-1]) * 3600
    if spec.endswith("m"):
        return time.time() - int(spec[:-1]) * 60
    try:
        return float(spec)
    except ValueError:
        pass
    # Try ISO format.
    import datetime as _dt
    try:
        return _dt.datetime.fromisoformat(spec.replace("Z", "+00:00")).timestamp()
    except ValueError:
        raise argparse.ArgumentTypeError(f"cannot parse --since {spec!r}")


if __name__ == "__main__":
    sys.exit(main())
