"""Memory layer + compression tests."""
from __future__ import annotations

from token_roi.compression import CompressionEngine
from token_roi.events import EventType, make_event
from token_roi.memory import MemoryEntry


def test_memory_entry_roundtrip(memory):
    entry = MemoryEntry(
        name="auth-rewrite",
        description="rewrite of auth middleware to remove session storage",
        type="project",
        body="Content goes here.\n\nSecond paragraph.",
        source_events=["evt_abc", "evt_def"],
    )
    path = memory.write_topic(entry)
    assert path.exists()
    reparsed = memory.read_topic(entry.name)
    assert reparsed.name == entry.name
    assert reparsed.description == entry.description
    assert reparsed.type == entry.type
    assert reparsed.source_events == entry.source_events


def test_memory_index_line_cap(memory):
    entries = [(f"title-{i}", f"topics/t{i}.md", "hook") for i in range(300)]
    memory.update_index(entries)
    lines = memory.index_lines()
    assert len(lines) == 200  # hard cap enforced


def test_compression_groups_similar_prompts(db, memory, store):
    """End-to-end: emit a handful of events, run compression, verify clusters."""
    sid = store.start_session()
    for ev in store.iter_session(sid):
        db.upsert_event(ev)

    def emit(type_, **payload):
        seq = store._next_seq(sid)
        ev = make_event(session_id=sid, seq=seq, type=type_, **payload)
        store.append(ev)
        db.upsert_event(ev)
        return ev

    # Two "auth rewrite" prompts + one unrelated prompt.
    p1 = emit(EventType.USER_PROMPT, payload={"text": "rewrite the auth middleware"})
    emit(EventType.ASSISTANT_MESSAGE, payload={"text": "plan laid out"},
         parent_ids=(p1.id,), tokens_in=50, tokens_out=100)
    p2 = emit(EventType.USER_PROMPT, payload={"text": "continue the auth middleware rewrite"})
    emit(EventType.ASSISTANT_MESSAGE, payload={"text": "next steps"},
         parent_ids=(p2.id,), tokens_in=50, tokens_out=100)
    p3 = emit(EventType.USER_PROMPT, payload={"text": "list my open PRs"})
    emit(EventType.ASSISTANT_MESSAGE, payload={"text": "..."},
         parent_ids=(p3.id,), tokens_in=30, tokens_out=40)

    engine = CompressionEngine(db, memory, session_id_for_log=sid)
    summary = engine.run(max_topics=10)

    # At least one topic file written; MEMORY.md has entries.
    assert summary["clusters"] >= 1
    assert memory.index_lines()
    assert list(memory.topics_dir.glob("*.md"))
