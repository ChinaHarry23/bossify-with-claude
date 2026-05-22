"""Tests for multi-platform importers."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from token_roi.events import EventType
from token_roi.importers import get_importer, list_sources
from token_roi.importers.aider import AiderImporter
from token_roi.importers.codex import CodexImporter
from token_roi.importers.cursor import CursorImporter
from token_roi.importers.openai_jsonl import OpenAIJsonlImporter


def _types(store, session_id) -> list[str]:
    return [e.type.value for e in store.iter_session(session_id)]


# ---------------------------------------------------------------- registry ----

def test_registry(store):
    sources = list_sources()
    assert {"claude-code", "codex", "cursor", "aider", "openai-jsonl"} <= set(sources)
    imp = get_importer("codex", store)
    assert isinstance(imp, CodexImporter)
    with pytest.raises(ValueError):
        get_importer("nonsense", store)


# ------------------------------------------------------------------- codex ----

def test_codex_basic(tmp_path: Path, store):
    sess = tmp_path / "abc123.jsonl"
    records = [
        {"type": "session_meta", "cwd": "/home/me/proj-foo"},
        {"type": "message", "role": "user", "content": "hi", "timestamp": 1},
        {"type": "token_count", "input_tokens": 10, "output_tokens": 5,
         "cached_input_tokens": 0},
        {"type": "message", "role": "assistant", "content": "hello", "timestamp": 2},
        {"type": "function_call", "name": "read_file",
         "arguments": json.dumps({"path": "a.py"}), "call_id": "c1"},
        {"type": "function_call_output", "call_id": "c1", "output": "contents"},
        {"type": "message", "role": "user", "content": "more", "timestamp": 3},
        {"type": "token_count", "input_tokens": 2, "output_tokens": 3,
         "cached_input_tokens": 1},
        {"type": "message", "role": "assistant", "content": "ok", "timestamp": 4},
        {"type": "garbage"},
    ]
    sess.write_text("\n".join(json.dumps(r) for r in records))

    imp = CodexImporter(store)
    stats = imp.import_path(sess)
    assert stats.user_prompts == 2
    assert stats.assistant_messages == 2
    assert stats.tool_uses == 1
    assert stats.tool_results == 1
    assert stats.file_reads == 1
    assert stats.skipped >= 1

    evs = store.iter_session("abc123")
    asst = [e for e in evs if e.type == EventType.ASSISTANT_MESSAGE]
    assert asst[0].tokens_in == 10 and asst[0].tokens_out == 5
    assert asst[1].tokens_in == 2 and asst[1].cached_tokens == 1


# ------------------------------------------------------------------ cursor ----

def test_cursor_basic(tmp_path: Path, store):
    ws = tmp_path / "ws_abc"
    ws.mkdir()
    (ws / "workspace.json").write_text(json.dumps({"folder": "file:///home/me/proj-bar"}))
    db_path = ws / "state.vscdb"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)")
    blob = {
        "tabs": [
            {
                "id": "conv-1",
                "messages": [
                    {"role": "user", "text": "hello", "timestamp": 1700000000000},
                    {"role": "assistant", "text": "hi there",
                     "timestamp": 1700000001000},
                ],
            }
        ]
    }
    conn.execute(
        "INSERT INTO ItemTable VALUES (?, ?)",
        ("workbench.panel.aichat.view.aichat.chatdata", json.dumps(blob)),
    )
    conn.commit()
    conn.close()

    imp = CursorImporter(store)
    stats = imp.import_path(tmp_path)
    assert stats.user_prompts == 1
    assert stats.assistant_messages == 1

    types = _types(store, "conv-1")
    assert EventType.USER_PROMPT.value in types
    assert EventType.ASSISTANT_MESSAGE.value in types


# ------------------------------------------------------ platform tagging ----

def test_platform_tagging_tags_each_importer(tmp_path: Path, store, db):
    """Every importer that has a DB handle should stamp its source_name
    onto session_summaries.platform so the manager dashboard can slice
    spend by tool."""
    from token_roi.importers.claude_code import ClaudeCodeImporter

    # Cursor session
    ws = tmp_path / "cursor-ws"
    ws.mkdir()
    (ws / "workspace.json").write_text(json.dumps({"folder": "file:///tmp/barproj"}))
    dbfile = ws / "state.vscdb"
    c = sqlite3.connect(dbfile)
    c.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)")
    c.execute(
        "INSERT INTO ItemTable VALUES (?, ?)",
        ("composer.composerData", json.dumps({"tabs": [{
            "id": "cur-A",
            "messages": [
                {"role": "user",      "text": "hi",  "timestamp": 1700000000000},
                {"role": "assistant", "text": "hey", "timestamp": 1700000001000},
            ],
        }]})),
    )
    c.commit(); c.close()
    CursorImporter(store, db=db).import_path(tmp_path)

    # Claude Code session
    cc_root = tmp_path / "cc"
    proj = cc_root / "-proj-slug"
    proj.mkdir(parents=True)
    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    (proj / f"{sid}.jsonl").write_text(json.dumps({
        "type": "assistant",
        "timestamp": "2026-01-01T00:00:00Z",
        "message": {"id": "m1", "model": "claude-sonnet-4-6-20260101",
                    "usage": {"input_tokens": 100, "output_tokens": 20},
                    "content": [{"type": "text", "text": "ok"}]},
        "uuid": "u1", "parentUuid": None,
    }) + "\n")
    ClaudeCodeImporter(store, db=db).import_path(cc_root)

    rows = {
        r["session_id"]: r["platform"]
        for r in db._conn.execute(
            "SELECT session_id, platform FROM session_summaries"
        ).fetchall()
    }
    assert rows["cur-A"] == "cursor"
    assert rows[sid] == "claude-code"


def test_cursor_usage_csv_import(tmp_path: Path, store, db):
    """Cursor's website CSV export carries real per-turn token counts —
    the only way subscription users can get priced Cursor activity
    into Bossify. Importer must: (1) skip errored/no-charge rows,
    (2) split Input with/without cache-write into tokens_in +
    cache_creation_tokens, (3) bucket turns into daily sessions, (4)
    tag everything with platform='cursor' so it merges with the
    state.vscdb bucket on the dashboard."""
    csv_path = tmp_path / "usage-events-2026-04-20.csv"
    csv_path.write_text(
        "Date,Cloud Agent ID,Automation ID,Kind,Model,Max Mode,"
        "Input (w/ Cache Write),Input (w/o Cache Write),Cache Read,"
        "Output Tokens,Total Tokens,Cost\n"
        '"2026-04-19T09:00:00Z","","","Included","claude-4.6-opus-high-thinking","Yes","1000","100","50000","500","51600","Included"\n'
        '"2026-04-19T10:00:00Z","","","Included","composer-2-fast","No","200","200","0","50","250","Included"\n'
        '"2026-04-18T22:00:00Z","","","Errored, No Charge","claude-4.6-opus","No","0","0","0","0","0","Included"\n'
        '"2026-04-18T21:00:00Z","","","Included","claude-4.5-sonnet","No","500","500","10000","100","10600","Included"\n'
    )

    from token_roi.importers.cursor_usage import CursorUsageImporter
    imp = CursorUsageImporter(store, db=db)
    stats = imp.import_path(csv_path)
    db.rebuild_from(store.iter_all_sessions())

    # 4 CSV rows: 3 real turns + 1 errored (skipped).
    assert stats.lines == 4
    assert stats.assistant_messages == 3
    assert stats.skipped == 1

    # Daily grouping → 2 sessions (2026-04-18 and 2026-04-19).
    sessions = sorted(set(
        r["session_id"] for r in db._conn.execute(
            "SELECT session_id FROM session_summaries WHERE platform = 'cursor'"
        ).fetchall()
    ))
    assert sessions == ["cursor-2026-04-18", "cursor-2026-04-19"]

    # Cache-write tokens = input_with_cache_write - input_without.
    # Row 1: 1000 - 100 = 900 cache_create, 100 tokens_in, 50000 cache_read, 500 out.
    ev_row1 = db._conn.execute(
        "SELECT tokens_in, tokens_out, cached_tokens, cache_creation_tokens, model "
        "FROM events WHERE session_id='cursor-2026-04-19' "
        "AND model='claude-4.6-opus-high-thinking'"
    ).fetchone()
    assert ev_row1["tokens_in"] == 100
    assert ev_row1["cache_creation_tokens"] == 900
    assert ev_row1["cached_tokens"] == 50000
    assert ev_row1["tokens_out"] == 500

    # platform_breakdown must bucket all under "cursor" (not
    # "cursor-usage" — that's the importer name, not the platform tag).
    pb = {p["platform"]: p for p in db.platform_breakdown()}
    assert "cursor" in pb
    assert "cursor-usage" not in pb
    assert pb["cursor"]["sessions"] == 2


def test_codex_desktop_envelope_format(tmp_path: Path, store, db):
    """Codex Desktop v0.107+ rollouts wrap records in an envelope:
    response_item.payload.type=="message" for turns and
    event_msg.payload.type=="token_count" for usage (with tokens nested
    under info.last_token_usage). Token_count also arrives AFTER the
    assistant message, not before. The importer's normalizer must
    flatten and reorder these so cost attaches to the right turn."""
    sess = tmp_path / "rollout-2026-03-28-abc.jsonl"
    records = [
        {"type": "session_meta", "timestamp": "2026-03-28T03:05:54Z",
         "payload": {"cwd": "/home/me/alpha", "id": "abc"}},
        {"type": "response_item", "timestamp": "2026-03-28T03:05:54Z",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "please do X"}]}},
        {"type": "event_msg", "timestamp": "2026-03-28T03:05:55Z",
         "payload": {"type": "task_started", "turn_id": "t1"}},
        {"type": "turn_context", "timestamp": "2026-03-28T03:05:55Z",
         "payload": {"turn_id": "t1", "model": "gpt-5.4"}},
        {"type": "response_item", "timestamp": "2026-03-28T03:05:56Z",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "done"}]}},
        # token_count AFTER the assistant message — importer must reorder.
        {"type": "event_msg", "timestamp": "2026-03-28T03:05:57Z",
         "payload": {"type": "token_count",
                     "info": {"last_token_usage":
                                  {"input_tokens": 9627,
                                   "output_tokens": 457,
                                   "cached_input_tokens": 6912,
                                   "reasoning_output_tokens": 361,
                                   "total_tokens": 10084}},
                     "rate_limits": {"plan_type": "plus"}}},
        # rate-limit-only token_count heartbeat with no info.last_token_usage —
        # must be dropped, not counted as zero-token usage.
        {"type": "event_msg", "timestamp": "2026-03-28T03:05:58Z",
         "payload": {"type": "token_count",
                     "info": None,
                     "rate_limits": {"plan_type": "plus"}}},
        {"type": "event_msg", "timestamp": "2026-03-28T03:05:59Z",
         "payload": {"type": "task_complete", "turn_id": "t1"}},
    ]
    sess.write_text("\n".join(json.dumps(r) for r in records))

    from token_roi.importers.codex import CodexImporter
    imp = CodexImporter(store, db=db)
    stats = imp.import_path(sess)

    assert stats.user_prompts == 1
    assert stats.assistant_messages == 1

    evs = list(store.iter_session("rollout-2026-03-28-abc"))
    asst = [e for e in evs if e.type == EventType.ASSISTANT_MESSAGE]
    assert len(asst) == 1
    assert asst[0].tokens_in == 9627
    assert asst[0].tokens_out == 457
    assert asst[0].cached_tokens == 6912
    assert asst[0].model == "gpt-5.4"

    # Project slug must come from session_meta.payload.cwd, not "codex-session".
    row = db._conn.execute(
        "SELECT project_slug, platform FROM session_summaries WHERE session_id = ?",
        ("rollout-2026-03-28-abc",),
    ).fetchone()
    assert row["project_slug"] == "alpha"
    assert row["platform"] == "codex"


def test_judge_platform_filter_scopes_prompts(tmp_path: Path, store, db):
    """prompts_needing_judgment(platform=X) must return only prompts whose
    session was imported via that platform. This is what lets nuclear
    judge each tool in its own progress block."""
    from token_roi.events import EventType, make_event
    from token_roi.llm_judge import Judge, LocalLLM

    # Seed two sessions with one user prompt each, tagged to different platforms.
    for sid, plat in [("cur-sess", "cursor"), ("cc-sess", "claude-code")]:
        store.start_session(sid)
        db.upsert_session_metadata(sid, project_slug=f"p-{plat}", platform=plat)
        store.append(make_event(
            session_id=sid, seq=store.next_seq(sid),
            type=EventType.USER_PROMPT,
            payload={"text": "please do X"},
            ts=1.0,
        ))
    db.rebuild_from(store.iter_all_sessions())

    judge = Judge(db, LocalLLM())
    assert len(judge.prompts_needing_judgment()) == 2
    cursor_rows = judge.prompts_needing_judgment(platform="cursor")
    claude_rows = judge.prompts_needing_judgment(platform="claude-code")
    assert [r["session_id"] for r in cursor_rows] == ["cur-sess"]
    assert [r["session_id"] for r in claude_rows] == ["cc-sess"]
    assert judge.prompts_needing_judgment(platform="aider") == []


def test_platform_breakdown_per_project(tmp_path: Path, store, db):
    """platform_breakdown should aggregate cost + sessions per platform,
    and scope to a single project_slug when given one."""
    from token_roi.importers.claude_code import ClaudeCodeImporter

    cc_root = tmp_path / "cc"
    proj = cc_root / "-shared-proj"
    proj.mkdir(parents=True)
    sid = "00000000-1111-2222-3333-444444444444"
    (proj / f"{sid}.jsonl").write_text(json.dumps({
        "type": "assistant",
        "timestamp": "2026-01-01T00:00:00Z",
        "message": {"id": "m1", "model": "claude-opus-4-7-20260101",
                    "usage": {"input_tokens": 1000, "output_tokens": 300},
                    "content": [{"type": "text", "text": "ok"}]},
        "uuid": "u1", "parentUuid": None,
    }) + "\n")
    ClaudeCodeImporter(store, db=db).import_path(cc_root)
    db.rebuild_from(store.iter_all_sessions())

    workspace = {p["platform"]: p for p in db.platform_breakdown()}
    assert "claude-code" in workspace
    assert workspace["claude-code"]["sessions"] == 1
    assert workspace["claude-code"]["cost_usd"] > 0

    scoped = db.platform_breakdown(project_slug="-shared-proj")
    assert len(scoped) == 1 and scoped[0]["platform"] == "claude-code"

    other = db.platform_breakdown(project_slug="nonexistent")
    assert other == []


# ------------------------------------------------------------------- aider ----

def test_aider_basic(tmp_path: Path, store):
    proj = tmp_path / "myproj"
    proj.mkdir()
    md = proj / ".aider.chat.history.md"
    md.write_text(
        "# aider chat started\n"
        "\n"
        "#### please add a hello function\n"
        "\n"
        "Sure, here is the change:\n"
        "\n"
        "```hello.py\n"
        "def hello():\n"
        "    print('hi')\n"
        "```\n"
        "\n"
        "#### thanks\n"
        "\n"
        "You're welcome.\n"
    )
    (proj / ".aider.llm.history").write_text(
        "TO LLM\n"
        "FROM LLM model=gpt-4.1\n"
        "prompt_tokens: 100\n"
        "completion_tokens: 20\n"
        "TO LLM\n"
        "FROM LLM model=gpt-4.1\n"
        "prompt_tokens: 5\n"
        "completion_tokens: 2\n"
    )
    imp = AiderImporter(store)
    stats = imp.import_path(proj)
    assert stats.user_prompts == 2
    assert stats.assistant_messages >= 2
    assert stats.file_writes == 1

    # find assistant with token enrichment
    sids = store.list_sessions()
    assert sids
    evs = store.iter_session(sids[0])
    asst = [e for e in evs if e.type == EventType.ASSISTANT_MESSAGE]
    assert any(e.tokens_in == 100 and e.tokens_out == 20 for e in asst)


# ----------------------------------------------------------- openai jsonl ----

def test_openai_jsonl_basic(tmp_path: Path, store):
    jsonl = tmp_path / "log.jsonl"
    line1 = {
        "model": "gpt-4.1",
        "created_at": 1700000000,
        "input": [{"role": "user", "content": "hello"}],
        "output": [
            {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "hi"}]},
            {"type": "function_call", "call_id": "c1",
             "name": "search", "arguments": json.dumps({"q": "foo"})},
        ],
        "usage": {"input_tokens": 12, "output_tokens": 4, "cached_input_tokens": 0},
    }
    line2 = {
        "model": "gpt-4.1",
        "created_at": 1700000001,
        "input": [
            {"type": "function_call_output", "call_id": "c1", "output": "result"},
            {"role": "user", "content": "next"},
        ],
        "output": [
            {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "done"}]},
        ],
        "usage": {"input_tokens": 3, "output_tokens": 2},
    }
    jsonl.write_text(json.dumps(line1) + "\n" + json.dumps(line2) + "\n")

    imp = OpenAIJsonlImporter(store)
    stats = imp.import_path(jsonl)
    assert stats.user_prompts == 2
    assert stats.assistant_messages == 2
    assert stats.tool_uses == 1
    assert stats.tool_results == 1

    evs = store.iter_session("log")
    asst = [e for e in evs if e.type == EventType.ASSISTANT_MESSAGE]
    assert asst[0].tokens_in == 12 and asst[0].tokens_out == 4
