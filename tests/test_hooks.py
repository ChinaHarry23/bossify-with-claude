"""Hook-layer integration tests.

The hook pipeline is the one place where Claude Code's raw payloads get
translated into ``token_roi`` events. Errors here are silent (hooks
swallow exceptions by design) so it's the single highest-leverage place
for a regression.
"""
from __future__ import annotations

from token_roi.events import EventType
from token_roi.hooks import (
    _infer_write_bytes,
    on_post_tool_use,
    on_pre_tool_use,
    on_user_prompt_submit,
)


# ---- _infer_write_bytes ----


def test_infer_bytes_write_tool_uses_content_field():
    assert _infer_write_bytes("Write", {"file_path": "/f", "content": "hi"}) == 2


def test_infer_bytes_edit_tool_uses_new_string():
    assert _infer_write_bytes("Edit", {"file_path": "/f", "new_string": "abcd"}) == 4


def test_infer_bytes_multiedit_sums_every_new_string():
    """A previous bug returned 0 for every MultiEdit because it looked for
    a top-level ``new_string`` instead of walking the ``edits`` list."""
    payload = {
        "file_path": "/f",
        "edits": [
            {"old_string": "a", "new_string": "hello"},           # 5
            {"old_string": "b", "new_string": "world!!"},         # 7
            {"old_string": "c", "new_string": ""},                # 0
        ],
    }
    assert _infer_write_bytes("MultiEdit", payload) == 12


def test_infer_bytes_notebook_edit_uses_new_source():
    assert _infer_write_bytes(
        "NotebookEdit", {"notebook_path": "/n.ipynb", "new_source": "print(1)"}
    ) == 8


def test_infer_bytes_handles_missing_content():
    assert _infer_write_bytes("Write", {"file_path": "/f"}) == 0
    assert _infer_write_bytes("MultiEdit", {"file_path": "/f"}) == 0


def test_infer_bytes_handles_non_string_content():
    # Tool payloads can occasionally carry non-strings (e.g. binary blobs
    # serialised oddly). We must not crash.
    assert _infer_write_bytes("Write", {"file_path": "/f", "content": 42}) == 0


# ---- end-to-end hook pipeline ----


def test_hook_pipeline_emits_file_write_with_correct_bytes(store):
    """UserPromptSubmit → PreToolUse → PostToolUse(Write) → FILE_WRITE event."""
    sid = "s_pipe"
    on_user_prompt_submit({"session_id": sid, "prompt": "do it"}, store)
    on_pre_tool_use({
        "session_id": sid,
        "tool_name": "Write",
        "tool_input": {"file_path": "/tmp/foo.py", "content": "print(1)"},
    }, store)
    on_post_tool_use({
        "session_id": sid,
        "tool_name": "Write",
        "tool_input": {"file_path": "/tmp/foo.py", "content": "print(1)"},
        "tool_response": {"ok": True},
        "success": True,
    }, store)

    events = list(store.iter_session(sid))
    types = [e.type for e in events]
    assert EventType.USER_PROMPT in types
    assert EventType.PRE_TOOL_USE in types
    assert EventType.POST_TOOL_USE in types
    assert EventType.FILE_WRITE in types

    fw = next(e for e in events if e.type is EventType.FILE_WRITE)
    assert fw.payload["path"] == "/tmp/foo.py"
    assert fw.payload["bytes"] == len("print(1)")


def test_hook_pipeline_tracks_multiedit_bytes(store):
    """MultiEdit-driven FILE_WRITE must carry the summed new_string bytes,
    not 0. Regression guard for the ROI-blind historical path."""
    sid = "s_multi"
    payload_input = {
        "file_path": "/tmp/bar.py",
        "edits": [
            {"old_string": "x", "new_string": "hello"},    # 5
            {"old_string": "y", "new_string": "world!"},   # 6
        ],
    }
    on_user_prompt_submit({"session_id": sid, "prompt": "refactor"}, store)
    on_post_tool_use({
        "session_id": sid,
        "tool_name": "MultiEdit",
        "tool_input": payload_input,
        "tool_response": {"ok": True},
        "success": True,
    }, store)

    events = list(store.iter_session(sid))
    fw = next(e for e in events if e.type is EventType.FILE_WRITE)
    assert fw.payload["bytes"] == 11


# ---- public next_seq alias ----


def test_store_exposes_public_next_seq(store):
    sid = store.start_session()
    a = store.next_seq(sid)
    b = store.next_seq(sid)
    assert b == a + 1
    # Legacy alias kept for backward compat (older callers used _next_seq).
    c = store._next_seq(sid)
    assert c == b + 1
