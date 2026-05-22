"""Append-only storage tests."""
from __future__ import annotations

from token_roi.events import EventType
from token_roi.storage import session


def test_append_and_read(store):
    sid = store.start_session()
    p = store.append_user_prompt(sid, "help me")
    a = store.append_assistant_message(sid, "ok", parent_ids=(p.id,),
                                        tokens_in=10, tokens_out=5)
    events = list(store.iter_session(sid))
    # SESSION_START, USER_PROMPT, ASSISTANT_MESSAGE
    assert [e.type for e in events] == [
        EventType.SESSION_START, EventType.USER_PROMPT, EventType.ASSISTANT_MESSAGE
    ]
    assert events[2].tokens_out == 5
    assert events[2].parent_ids == (p.id,)


def test_sessions_context_manager_emits_end(store):
    with session(store) as sid:
        store.append_user_prompt(sid, "hi")
    events = list(store.iter_session(sid))
    assert events[0].type is EventType.SESSION_START
    assert events[-1].type is EventType.SESSION_END


def test_malformed_jsonl_is_skipped(store, data_dir):
    sid = store.start_session()
    store.append_user_prompt(sid, "first")
    # Corrupt a single line in the file: write a half-JSON line.
    import datetime as dt
    day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    path = data_dir / "raw_events" / day / f"session_{sid}.jsonl"
    with path.open("a") as f:
        f.write('{"id": "garbage"\n')  # deliberately malformed
    store.append_user_prompt(sid, "second")
    events = list(store.iter_session(sid))
    # We should still see SESSION_START + two valid prompts; bad line skipped.
    types = [e.type for e in events]
    assert EventType.USER_PROMPT in types
    assert types.count(EventType.USER_PROMPT) == 2


def test_append_is_atomic_under_concurrent_writes(data_dir):
    """fcntl.flock should keep concurrent appends from interleaving lines.

    We exercise with threads; full multi-process testing would be overkill
    for the test suite but the lock is process-level as well.
    """
    import threading
    from token_roi.storage import EventStore
    store = EventStore(data_dir)
    sid = store.start_session()
    N = 50

    def writer(ix: int):
        for i in range(N):
            store.append_user_prompt(sid, f"writer_{ix}_msg_{i}")

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    events = list(store.iter_session(sid))
    prompt_events = [e for e in events if e.type is EventType.USER_PROMPT]
    # Should have exactly 4 * N prompts with no corrupted lines.
    assert len(prompt_events) == 4 * N
