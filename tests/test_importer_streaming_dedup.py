"""Regression test for the streaming-duplicate fix in the Claude Code importer.

Claude Code writes one JSONL record per content block (thinking / text /
tool_use), and *replicates* the same cumulative `usage` block on every
record. Before the fix, this multiplied token totals by 2-5x for any turn
with tool calls. The fix: only the first record for a given Anthropic
`message.id` carries the usage block; subsequent records for the same
message.id emit with zero tokens.
"""
from __future__ import annotations

import json
from pathlib import Path

from token_roi.importers.claude_code import ClaudeCodeImporter
from token_roi.storage import EventStore


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def test_streaming_fragments_counted_once(tmp_path):
    """One Anthropic turn split into 5 JSONL records bills tokens once."""
    session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    jsonl = tmp_path / f"{session_id}.jsonl"
    # A single Anthropic turn (message.id=msg_1) emitted as 5 JSONL
    # records: thinking block, text block, and three tool_use blocks.
    # Each record carries the SAME usage — this is what Claude Code
    # actually writes when streaming an assistant turn.
    usage = {
        "input_tokens": 100,
        "output_tokens": 500,
        "cache_read_input_tokens": 10_000,
        "cache_creation_input_tokens": 2_000,
    }
    base = {
        "type": "assistant",
        "parentUuid": None,
        "timestamp": "2026-04-18T10:00:00.000Z",
        "message": {"id": "msg_1", "model": "claude-opus-4-7", "usage": usage},
    }
    records = [
        {**base, "uuid": "u1",
         "message": {**base["message"], "content": [{"type": "thinking", "thinking": "..."}]}},
        {**base, "uuid": "u2",
         "message": {**base["message"], "content": [{"type": "text", "text": "Hi"}]}},
        {**base, "uuid": "u3",
         "message": {**base["message"], "content": [{"type": "tool_use", "id": "tu_1", "name": "Bash", "input": {"cmd": "ls"}}]}},
        {**base, "uuid": "u4",
         "message": {**base["message"], "content": [{"type": "tool_use", "id": "tu_2", "name": "Bash", "input": {"cmd": "pwd"}}]}},
        {**base, "uuid": "u5",
         "message": {**base["message"], "content": [{"type": "tool_use", "id": "tu_3", "name": "Bash", "input": {"cmd": "date"}}]}},
    ]
    _write_jsonl(jsonl, records)

    store = EventStore(tmp_path)
    ClaudeCodeImporter(store).import_path(jsonl)

    # Read events back and sum tokens across all assistant_messages.
    events = list(store.iter_session(session_id))
    asst = [e for e in events if e.type.value == "assistant_message"]
    # Still one event per record (preserves tool-use chaining), but only
    # one of them carries the full usage block.
    assert len(asst) == 5, f"expected 5 assistant_message events, got {len(asst)}"
    assert sum(e.tokens_in for e in asst) == 100
    assert sum(e.tokens_out for e in asst) == 500
    assert sum(e.cached_tokens for e in asst) == 10_000
    assert sum(e.cache_creation_tokens for e in asst) == 2_000
    # Exactly one event carries tokens; the other four are zero.
    nonzero = [e for e in asst if e.tokens_in or e.tokens_out
                                  or e.cached_tokens or e.cache_creation_tokens]
    assert len(nonzero) == 1


def test_distinct_message_ids_each_count(tmp_path):
    """Two different turns (distinct message.ids) both bill independently."""
    session_id = "11111111-2222-3333-4444-555555555555"
    jsonl = tmp_path / f"{session_id}.jsonl"
    usage_a = {"input_tokens": 10, "output_tokens": 20,
               "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    usage_b = {"input_tokens": 7, "output_tokens": 3,
               "cache_read_input_tokens": 100, "cache_creation_input_tokens": 50}
    records = [
        {"type": "assistant", "uuid": "a1", "parentUuid": None,
         "timestamp": "2026-04-18T10:00:00.000Z",
         "message": {"id": "msg_A", "model": "claude-opus-4-7",
                     "usage": usage_a,
                     "content": [{"type": "text", "text": "turn A"}]}},
        {"type": "assistant", "uuid": "b1", "parentUuid": None,
         "timestamp": "2026-04-18T10:00:01.000Z",
         "message": {"id": "msg_B", "model": "claude-opus-4-7",
                     "usage": usage_b,
                     "content": [{"type": "text", "text": "turn B"}]}},
    ]
    _write_jsonl(jsonl, records)

    store = EventStore(tmp_path)
    ClaudeCodeImporter(store).import_path(jsonl)

    events = list(store.iter_session(session_id))
    asst = [e for e in events if e.type.value == "assistant_message"]
    assert len(asst) == 2
    assert sum(e.tokens_in for e in asst) == 17
    assert sum(e.tokens_out for e in asst) == 23
    assert sum(e.cached_tokens for e in asst) == 100
    assert sum(e.cache_creation_tokens for e in asst) == 50
