"""Claude Code hook integration (Mode A).

Claude Code hooks are shell commands run by the harness at lifecycle points.
Each hook receives a JSON payload on stdin with the current event. We read
that payload, derive token_roi events, and append them to the event store.

Supported hook types (documented at https://docs.claude.com/claude-code/hooks):
    - UserPromptSubmit   → USER_PROMPT event
    - PreToolUse         → PRE_TOOL_USE event
    - PostToolUse        → POST_TOOL_USE + derived FILE_READ/FILE_WRITE
                           + MEMORY_WRITE if path lives under data/memory/
    - Stop               → ASSISTANT_MESSAGE event (with usage if provided)
    - SessionStart       → SESSION_START
    - SessionEnd         → SESSION_END

Each hook entry point is `on_<hook_name>(payload, store, memory)` — the
actual hook scripts under `hooks/` are thin shims that call these.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from .events import EventType, make_event
from .memory import MemoryLayer
from .storage import EventStore

log = logging.getLogger(__name__)


def load_payload() -> dict[str, Any]:
    """Read the hook payload from stdin.

    Claude Code always passes JSON on stdin. If stdin is empty (e.g. dry
    run), return an empty dict rather than crashing.
    """
    if sys.stdin.isatty():
        return {}
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        log.error("hook payload not JSON: %s", e)
        return {}


def _session_id(payload: dict) -> str:
    # Claude Code uses "session_id"; SDK uses "sessionId". Support both.
    return (
        payload.get("session_id")
        or payload.get("sessionId")
        or payload.get("session", {}).get("id")
        or "unknown"
    )


def on_user_prompt_submit(payload: dict, store: EventStore) -> None:
    sid = store.start_session(_session_id(payload))
    text = payload.get("prompt") or payload.get("user_message") or ""
    store.append_user_prompt(sid, text)


def on_pre_tool_use(payload: dict, store: EventStore) -> None:
    sid = store.start_session(_session_id(payload))
    tool_name = payload.get("tool_name") or payload.get("tool", {}).get("name") or "unknown"
    tool_input = payload.get("tool_input") or payload.get("input") or {}
    store.append(make_event(
        session_id=sid, seq=store._next_seq(sid),
        type=EventType.PRE_TOOL_USE,
        payload={"tool_name": tool_name, "input": tool_input},
    ))


def on_post_tool_use(payload: dict, store: EventStore, memory: MemoryLayer | None = None) -> None:
    sid = store.start_session(_session_id(payload))
    tool_name = payload.get("tool_name") or payload.get("tool", {}).get("name") or "unknown"
    tool_input = payload.get("tool_input") or payload.get("input") or {}
    tool_output = payload.get("tool_response") or payload.get("output") or {}
    success = bool(payload.get("success", True))

    post = store.append(make_event(
        session_id=sid, seq=store._next_seq(sid),
        type=EventType.POST_TOOL_USE,
        payload={
            "tool_name": tool_name,
            "input": tool_input,
            "output": _truncate_for_log(tool_output),
            "success": success,
        },
    ))

    # Promote file/memory touches to their own typed events so ROI queries
    # don't have to parse tool payloads.
    _promote_file_touch(store, sid, tool_name, tool_input, tool_output, memory, parent=post.id)


def _promote_file_touch(
    store: EventStore,
    sid: str,
    tool_name: str,
    tool_input: dict,
    tool_output: Any,
    memory: MemoryLayer | None,
    *,
    parent: str,
) -> None:
    path = None
    bytes_touched = 0
    is_write = False
    if tool_name in {"Read", "NotebookRead"}:
        path = tool_input.get("file_path")
    elif tool_name in {"Write", "Edit", "NotebookEdit", "MultiEdit"}:
        path = tool_input.get("file_path")
        is_write = True
        content = tool_input.get("new_string") or tool_input.get("content") or ""
        bytes_touched = len(content.encode("utf-8")) if isinstance(content, str) else 0
    if not path:
        return

    is_memory = memory is not None and str(path).startswith(str(memory.root))
    import hashlib
    h = hashlib.sha256(str(tool_output)[:4096].encode("utf-8")).hexdigest()[:16] if tool_output else ""

    if is_memory and is_write:
        store.append(make_event(
            session_id=sid, seq=store._next_seq(sid),
            type=EventType.MEMORY_WRITE,
            payload={"path": path, "content_hash": h, "bytes": bytes_touched,
                     "kind": "agent_edit"},
            parent_ids=(parent,),
        ))
    elif is_memory and not is_write:
        store.append(make_event(
            session_id=sid, seq=store._next_seq(sid),
            type=EventType.MEMORY_READ,
            payload={"path": path, "content_hash": h, "bytes": bytes_touched},
            parent_ids=(parent,),
        ))
    elif is_write:
        store.append(make_event(
            session_id=sid, seq=store._next_seq(sid),
            type=EventType.FILE_WRITE,
            payload={"path": path, "content_hash": h, "bytes": bytes_touched},
            parent_ids=(parent,),
        ))
    else:
        store.append(make_event(
            session_id=sid, seq=store._next_seq(sid),
            type=EventType.FILE_READ,
            payload={"path": path, "content_hash": h, "bytes": bytes_touched},
            parent_ids=(parent,),
        ))


def on_stop(payload: dict, store: EventStore) -> None:
    """Emitted when the assistant finishes producing a response."""
    sid = store.start_session(_session_id(payload))
    text = payload.get("response") or payload.get("text") or ""
    usage = payload.get("usage") or {}
    store.append_assistant_message(
        sid, text,
        tokens_in=int(usage.get("input_tokens") or 0),
        tokens_out=int(usage.get("output_tokens") or 0),
        cached_tokens=int(usage.get("cache_read_input_tokens") or 0),
        cache_creation_tokens=int(usage.get("cache_creation_input_tokens") or 0),
        model=payload.get("model"),
        latency_ms=payload.get("latency_ms"),
    )


def on_session_start(payload: dict, store: EventStore) -> None:
    store.start_session(_session_id(payload))


def on_session_end(payload: dict, store: EventStore) -> None:
    store.end_session(_session_id(payload))


def _truncate_for_log(obj: Any, max_chars: int = 4096) -> Any:
    """Avoid logging megabytes of tool output into JSONL.

    Keep the structure but replace long strings with a {truncated: N} marker.
    """
    if isinstance(obj, str):
        if len(obj) <= max_chars:
            return obj
        return obj[:max_chars] + f"... [truncated {len(obj) - max_chars} chars]"
    if isinstance(obj, list):
        return [_truncate_for_log(x, max_chars) for x in obj[:50]]
    if isinstance(obj, dict):
        return {k: _truncate_for_log(v, max_chars) for k, v in list(obj.items())[:50]}
    return obj


# ---- install helper ----

HOOK_SCRIPT_NAMES = {
    "UserPromptSubmit": "user_prompt_submit.py",
    "PreToolUse":       "pre_tool_use.py",
    "PostToolUse":      "post_tool_use.py",
    "Stop":             "stop.py",
    "SessionStart":     "session_start.py",
    "SessionEnd":       "session_end.py",
}


def install_into_settings(
    settings_path: Path,
    *,
    hooks_dir: Path,
    data_dir: Path,
    python: str | None = None,
) -> dict:
    """Merge token-roi hook invocations into the user's settings.json.

    Idempotent: running twice produces the same file. Writes a .bak next to
    settings.json on first run so the user can revert.
    """
    py = python or sys.executable
    data_dir = data_dir.resolve()
    hooks_dir = hooks_dir.resolve()

    if settings_path.exists():
        backup = settings_path.with_suffix(settings_path.suffix + ".bak")
        if not backup.exists():
            backup.write_bytes(settings_path.read_bytes())
        current = json.loads(settings_path.read_text(encoding="utf-8"))
    else:
        current = {}

    hooks_cfg = current.setdefault("hooks", {})

    for hook_name, script in HOOK_SCRIPT_NAMES.items():
        script_path = hooks_dir / script
        if not script_path.exists():
            continue
        cmd = f'{py} "{script_path}" --data-dir "{data_dir}"'
        entries = hooks_cfg.setdefault(hook_name, [])
        # Skip if we already installed an equivalent command.
        if any(
            e.get("hooks", [{}])[0].get("command", "").startswith(f'{py} "{script_path}"')
            for e in entries
            if isinstance(e, dict)
        ):
            continue
        entries.append({
            "matcher": "*",
            "hooks": [{"type": "command", "command": cmd}],
        })

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(current, indent=2), encoding="utf-8")
    return current
