"""Import Claude Code session history into token_roi events.

Claude Code persists every session as JSONL under
    ~/.claude/projects/<slugged-cwd>/<session-uuid>.jsonl

Each line is one of:

    { type: "user",      message: {role, content}, uuid, parentUuid, ...}
    { type: "assistant", message: {role, content, usage, model}, uuid, parentUuid, ...}
    { type: "queue-operation" | "attachment" | "system", ...}        # skipped / metadata

`message.content` is either a string (user turn) or a list of blocks
(assistant turn, or user tool-result turn). Blocks:

    {type: "text",        text}
    {type: "tool_use",    id, name, input}
    {type: "tool_result", tool_use_id, content, is_error}

The importer walks each JSONL in timestamp order and emits typed token_roi
events:

    queue-operation / system         -> skipped
    user (string content)            -> USER_PROMPT
    assistant (text + tool_use)      -> ASSISTANT_MESSAGE (+ PRE_TOOL_USE per tool_use)
    user (tool_result blocks)        -> POST_TOOL_USE (+ FILE_READ/WRITE for Read/Write/Edit)

Idempotency: event ids are content-addressed by (session_id, seq, type,
payload). Re-running the import after new sessions are appended produces
the same ids for unchanged events and new ids for new ones. A re-import of
the same file is safe (duplicates are filtered by id in the DB layer).

The importer does NOT clobber the existing event store. It appends. If you
want a clean slate, delete `data/raw_events/` first — but the whole point of
append-only ground truth is that you rarely should.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from ..db import AnalyticsDB
from ..employees import EmployeeRegistry
from ..events import EventType, is_synthetic_prompt, make_event
from ..storage import EventStore
from . import ImportStats, Importer, register

log = logging.getLogger(__name__)


# Tool names that get promoted into typed file events on PostToolUse.
_READ_TOOLS = {"Read", "NotebookRead"}
_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


@register
class ClaudeCodeImporter(Importer):
    """Imports one or more Claude Code session JSONL files.

    Usage:
        imp = ClaudeCodeImporter(store)
        stats = imp.import_path(Path.home() / ".claude/projects")
    """

    source_name = "claude-code"

    @classmethod
    def default_path(cls) -> Path:
        return Path("~/.claude/projects").expanduser()

    def __init__(
        self,
        store: EventStore,
        *,
        db: AnalyticsDB | None = None,
        employees: EmployeeRegistry | None = None,
    ):
        self.store = store
        # Optional — when provided, the importer tags each session with
        # its project slug + resolved employee id in session_summaries.
        # CLI wires both; library-only callers can omit them.
        self.db = db
        self.employees = employees

    # ---- public ----

    def import_path(self, path: Path | str, *, project_filter: str | None = None) -> ImportStats:
        """Import every JSONL reachable under `path`.

        `path` can be:
            - a single JSONL file    -> import it
            - a project directory    -> import every *.jsonl in it
            - the projects root      -> recurse into each project directory
        """
        p = Path(path).expanduser()
        stats = ImportStats()
        if p.is_file() and p.suffix == ".jsonl":
            self._import_file(p, stats)
        elif p.is_dir():
            files = sorted(p.rglob("*.jsonl"))
            if project_filter:
                files = [f for f in files if project_filter in str(f)]
            for f in files:
                self._import_file(f, stats)
        else:
            raise FileNotFoundError(f"no JSONL found at {p}")
        return stats

    # ---- internals ----

    def _import_file(self, path: Path, stats: ImportStats) -> None:
        """Import one session file. Session id := the file stem (UUID).

        Claude Code files sometimes have more than one sessionId inside
        (resumption), but the *file* already scopes to one on-disk session
        and the overlap is minor. We trust the file name for grouping.
        """
        stats.files += 1
        session_id = path.stem  # UUID
        # Start/resume the session. start_session is idempotent — if the
        # session file already exists in our store, this just advances seq.
        self.store.start_session(session_id)

        # Project slug = the parent directory name inside ~/.claude/projects/.
        # This is how Claude Code encodes the original cwd, and it's the
        # only stable identifier we can extract without parsing every event.
        project_slug = path.parent.name
        employee_id = None
        if self.employees is not None:
            employee_id = self.employees.resolve_for_slug(project_slug).id
        if self.db is not None:
            self.db.upsert_session_metadata(
                session_id,
                project_slug=project_slug,
                employee_id=employee_id,
            )

        # First pass: read + parse + timestamp-sort. Claude Code writes in
        # order, but some lines share timestamps, and we want deterministic
        # output even so. Break ties by file position.
        raw_lines: list[tuple[int, dict]] = []
        for pos, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines()):
            stats.lines += 1
            line = line.strip()
            if not line:
                continue
            try:
                raw_lines.append((pos, json.loads(line)))
            except json.JSONDecodeError as e:
                log.warning("skip malformed line %s:%d: %s", path, pos, e)
                stats.skipped += 1

        raw_lines.sort(key=lambda t: (_ts_of(t[1]), t[0]))

        # Build a uuid -> our emitted event id map so we can set parent_ids
        # correctly when the Claude Code record carries a parentUuid.
        # For tool_result → tool_use pairing, keep a separate map keyed by
        # the Anthropic tool_use.id; the value is the (pre_event_id,
        # tool_name, tool_input) triple so we can emit POST and promoted
        # file events in O(1) without rescanning the session.
        uuid_to_event_id: dict[str, str] = {}
        tool_use_id_to_pre_event: dict[str, dict] = {}
        # Claude Code splits one Anthropic assistant turn across N JSONL
        # records — one per content block (thinking / text / tool_use) —
        # and replicates the same cumulative `usage` block on every one.
        # Counting each copy multiplies the true cost 2-5x for tool-heavy
        # turns. We track which message.ids have already consumed the
        # usage so only the first emitted event per turn carries it.
        seen_message_ids: set[str] = set()

        for _, rec in raw_lines:
            t = rec.get("type")
            if t in {"queue-operation", "attachment", "system"}:
                stats.skipped += 1
                continue
            if t == "user":
                self._emit_user(rec, session_id, uuid_to_event_id,
                                tool_use_id_to_pre_event, stats)
            elif t == "assistant":
                self._emit_assistant(rec, session_id, uuid_to_event_id,
                                     tool_use_id_to_pre_event,
                                     seen_message_ids, stats)
            else:
                stats.skipped += 1

    # ---- user records ----

    def _emit_user(
        self,
        rec: dict,
        session_id: str,
        uuid_to_event_id: dict[str, str],
        tool_use_id_to_pre_event: dict[str, str],
        stats: ImportStats,
    ) -> None:
        msg = rec.get("message") or {}
        content = msg.get("content")
        parent_uuid = rec.get("parentUuid")
        rec_uuid = rec.get("uuid") or ""
        ts = _ts_of(rec)

        if isinstance(content, str):
            # Drop Claude Code plumbing (slash-command wrappers, task
            # notifications, post-compaction continuations). These look
            # like user prompts but carry no intent — judging them inflates
            # the WASTED column with fake zero-cost rows.
            if is_synthetic_prompt(content):
                stats.synthetic_prompts_dropped += 1
                return
            # Real user prompt.
            parent_ids = _parent_tuple(parent_uuid, uuid_to_event_id)
            ev = self.store.append(make_event(
                session_id=session_id,
                seq=self.store.next_seq(session_id),
                type=EventType.USER_PROMPT,
                payload={"text": content},
                parent_ids=parent_ids,
                ts=ts,
            ))
            uuid_to_event_id[rec_uuid] = ev.id
            stats.user_prompts += 1
            stats.events_written += 1
            return

        if isinstance(content, list):
            # Tool-result user turn. Each tool_result block pairs back to a
            # previously-emitted PRE_TOOL_USE via tool_use_id.
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "tool_result":
                    continue
                stats.tool_results += 1
                tool_use_id = block.get("tool_use_id") or ""
                meta = tool_use_id_to_pre_event.get(tool_use_id) or {}
                pre_event_id = meta.get("pre_event_id", "")
                tool_name = meta.get("tool_name") or "unknown"
                pre_input = meta.get("tool_input") or {}
                output = block.get("content")
                success = not bool(block.get("is_error"))
                post = self.store.append(make_event(
                    session_id=session_id,
                    seq=self.store._next_seq(session_id),
                    type=EventType.POST_TOOL_USE,
                    payload={
                        "tool_name": tool_name,
                        "output": _truncate(output),
                        "success": success,
                    },
                    parent_ids=(pre_event_id,) if pre_event_id else (),
                    ts=ts,
                ))
                uuid_to_event_id[rec_uuid] = post.id
                stats.events_written += 1
                # Promote FILE_READ / FILE_WRITE if applicable.
                if pre_input:
                    self._promote_file_event(
                        session_id, tool_name, pre_input, output,
                        parent_id=post.id, stats=stats, ts=ts,
                    )
            return

        # Unknown user shape — skip.
        stats.skipped += 1

    # ---- assistant records ----

    def _emit_assistant(
        self,
        rec: dict,
        session_id: str,
        uuid_to_event_id: dict[str, str],
        tool_use_id_to_pre_event: dict[str, str],
        seen_message_ids: set[str],
        stats: ImportStats,
    ) -> None:
        msg = rec.get("message") or {}
        content = msg.get("content") or []
        model = msg.get("model")
        parent_uuid = rec.get("parentUuid")
        rec_uuid = rec.get("uuid") or ""
        ts = _ts_of(rec)
        parent_ids = _parent_tuple(parent_uuid, uuid_to_event_id)

        # Only the *first* JSONL record we see for a given Anthropic
        # message.id carries the usage block. Subsequent records are
        # streaming fragments of the same turn, and their `usage` is a
        # duplicate of the first — counting it again multiplies the real
        # cost. tool_use ids across fragments are still distinct, so we
        # keep emitting one event per record (to preserve tool-call
        # chaining); we just zero out tokens after the first.
        mid = msg.get("id") or ""
        if mid and mid in seen_message_ids:
            usage: dict = {}
        else:
            usage = msg.get("usage") or {}
            if mid:
                seen_message_ids.add(mid)

        text_parts: list[str] = []
        tool_uses: list[dict] = []
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text") or "")
            elif btype == "tool_use":
                tool_uses.append(block)

        text = "\n".join(t for t in text_parts if t)
        asst_ev = self.store.append(make_event(
            session_id=session_id,
            seq=self.store._next_seq(session_id),
            type=EventType.ASSISTANT_MESSAGE,
            payload={"text": text},
            parent_ids=parent_ids,
            tokens_in=int(usage.get("input_tokens") or 0),
            tokens_out=int(usage.get("output_tokens") or 0),
            cached_tokens=int(usage.get("cache_read_input_tokens") or 0),
            cache_creation_tokens=int(usage.get("cache_creation_input_tokens") or 0),
            model=model,
            ts=ts,
        ))
        uuid_to_event_id[rec_uuid] = asst_ev.id
        stats.assistant_messages += 1
        stats.events_written += 1

        for tu in tool_uses:
            tool_name = tu.get("name") or "unknown"
            tool_input = tu.get("input") or {}
            pre = self.store.append(make_event(
                session_id=session_id,
                seq=self.store._next_seq(session_id),
                type=EventType.PRE_TOOL_USE,
                payload={"tool_name": tool_name, "input": tool_input},
                parent_ids=(asst_ev.id,),
                ts=ts,
            ))
            # Persist a mapping: Anthropic tool_use.id -> pre event metadata.
            # Storing name + input here avoids a per-result rescan of the
            # session when the matching POST_TOOL_USE is emitted.
            tuid = tu.get("id") or ""
            if tuid:
                tool_use_id_to_pre_event[tuid] = {
                    "pre_event_id": pre.id,
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                }
            stats.tool_uses += 1
            stats.events_written += 1

    # ---- promotion helpers ----

    def _promote_file_event(
        self,
        session_id: str,
        tool_name: str,
        tool_input: dict,
        tool_output: object,
        *,
        parent_id: str,
        stats: ImportStats,
        ts: float,
    ) -> None:
        path = tool_input.get("file_path")
        if not path:
            return
        content_str = _stringify(tool_output)[:8192]
        h = hashlib.sha256(content_str.encode("utf-8")).hexdigest()[:16] if content_str else ""
        if tool_name in _READ_TOOLS:
            self.store.append(make_event(
                session_id=session_id,
                seq=self.store._next_seq(session_id),
                type=EventType.FILE_READ,
                payload={"path": path, "content_hash": h, "bytes": len(content_str)},
                parent_ids=(parent_id,),
                ts=ts,
            ))
            stats.file_reads += 1
            stats.events_written += 1
        elif tool_name in _WRITE_TOOLS:
            content = tool_input.get("new_string") or tool_input.get("content") or ""
            bytes_ = len(content.encode("utf-8")) if isinstance(content, str) else 0
            self.store.append(make_event(
                session_id=session_id,
                seq=self.store._next_seq(session_id),
                type=EventType.FILE_WRITE,
                payload={"path": path, "content_hash": h, "bytes": bytes_},
                parent_ids=(parent_id,),
                ts=ts,
            ))
            stats.file_writes += 1
            stats.events_written += 1


# ---- helpers ----

def _ts_of(rec: dict) -> float:
    ts = rec.get("timestamp")
    if not ts:
        return 0.0
    try:
        # Claude Code uses ISO8601 with 'Z'. Convert to epoch seconds.
        return _dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _parent_tuple(parent_uuid: str | None, uuid_to_event_id: dict[str, str]) -> tuple[str, ...]:
    if not parent_uuid:
        return ()
    eid = uuid_to_event_id.get(parent_uuid)
    return (eid,) if eid else ()


def _stringify(v: object) -> str:
    if isinstance(v, str):
        return v
    try:
        return json.dumps(v, ensure_ascii=False, default=str)
    except Exception:
        return str(v)


def _truncate(v: object, n: int = 4096) -> object:
    if isinstance(v, str) and len(v) > n:
        return v[:n] + f"... [truncated {len(v) - n} chars]"
    if isinstance(v, list):
        return [_truncate(x, n) for x in v[:50]]
    if isinstance(v, dict):
        return {k: _truncate(val, n) for k, val in list(v.items())[:50]}
    return v


