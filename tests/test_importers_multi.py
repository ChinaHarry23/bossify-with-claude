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
