"""Import OpenAI Codex CLI session logs.

Codex CLI (github.com/openai/codex) persists each session as a JSONL file
under ``~/.codex/sessions/``. Each line is a record of one of these types
(schema varies slightly across Codex versions; this importer is lenient):

    {type: "message",             role: "user"|"assistant", content: "..."}
    {type: "function_call",       name: "...", arguments: "...", call_id: "..."}
    {type: "function_call_output",call_id: "...", output: "..."}
    {type: "token_count",         input_tokens, output_tokens, cached_input_tokens}
    {type: "session_meta",        cwd: "/path/to/project", ...}

Mapping:
    user message             -> USER_PROMPT
    assistant message        -> ASSISTANT_MESSAGE (tokens from preceding
                                token_count record)
    function_call            -> PRE_TOOL_USE (+ FILE_READ/FILE_WRITE when
                                the tool is read_file/apply_patch/shell and
                                a path can be inferred from arguments)
    function_call_output     -> POST_TOOL_USE (paired by call_id, else by
                                position to the most recent unpaired PRE)
    anything else            -> stats.skipped
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import re
from pathlib import Path

from ..events import EventType, is_synthetic_prompt, make_event
from . import ImportStats, Importer, register

log = logging.getLogger(__name__)


@register
class CodexImporter(Importer):
    source_name = "codex"

    @classmethod
    def default_path(cls) -> Path:
        return Path("~/.codex/sessions").expanduser()

    def import_path(
        self, path: Path | str, *, project_filter: str | None = None
    ) -> ImportStats:
        p = Path(path).expanduser()
        stats = ImportStats()
        if p.is_file() and p.suffix == ".jsonl":
            files = [p]
        elif p.is_dir():
            files = sorted(p.rglob("*.jsonl"))
        else:
            raise FileNotFoundError(f"no Codex JSONL found at {p}")
        for f in files:
            if project_filter and project_filter not in str(f):
                continue
            self._import_file(f, stats)
        return stats

    # ---- internals ----

    def _import_file(self, path: Path, stats: ImportStats) -> None:
        stats.files += 1
        session_id = path.stem
        self.store.start_session(session_id)

        records: list[dict] = []
        for pos, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines()
        ):
            stats.lines += 1
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as e:
                log.warning("skip malformed line %s:%d: %s", path, pos, e)
                stats.skipped += 1

        # Extract cwd from first record that carries it.
        project_slug = "codex-session"
        for r in records:
            cwd = r.get("cwd") or (r.get("payload") or {}).get("cwd")
            if cwd:
                project_slug = Path(str(cwd)).name or "codex-session"
                break

        employee_id = None
        if self.employees is not None:
            employee_id = self.employees.resolve_for_slug(project_slug).id
        if self.db is not None:
            self.db.upsert_session_metadata(
                session_id, project_slug=project_slug, employee_id=employee_id,
            )

        # Walk records, tracking nearest preceding token_count and open
        # function_calls by call_id for pairing.
        pending_usage: dict = {}
        open_calls: dict[str, str] = {}   # call_id -> pre_event_id
        open_calls_meta: dict[str, dict] = {}
        fifo_unpaired: list[str] = []      # call_ids awaiting output

        for rec in records:
            t = rec.get("type")
            ts = _ts(rec)
            if t == "token_count":
                pending_usage = {
                    "tokens_in": int(rec.get("input_tokens") or 0),
                    "tokens_out": int(rec.get("output_tokens") or 0),
                    "cached_tokens": int(rec.get("cached_input_tokens") or 0),
                    "model": rec.get("model"),
                }
                continue
            if t == "message":
                role = rec.get("role")
                content = _content_text(rec.get("content"))
                if role == "user":
                    if is_synthetic_prompt(content):
                        stats.synthetic_prompts_dropped += 1
                        continue
                    self.store.append(make_event(
                        session_id=session_id,
                        seq=self.store.next_seq(session_id),
                        type=EventType.USER_PROMPT,
                        payload={"text": content},
                        ts=ts,
                    ))
                    stats.user_prompts += 1
                    stats.events_written += 1
                elif role == "assistant":
                    self.store.append(make_event(
                        session_id=session_id,
                        seq=self.store.next_seq(session_id),
                        type=EventType.ASSISTANT_MESSAGE,
                        payload={"text": content},
                        tokens_in=pending_usage.get("tokens_in", 0),
                        tokens_out=pending_usage.get("tokens_out", 0),
                        cached_tokens=pending_usage.get("cached_tokens", 0),
                        model=pending_usage.get("model"),
                        ts=ts,
                    ))
                    pending_usage = {}
                    stats.assistant_messages += 1
                    stats.events_written += 1
                else:
                    stats.skipped += 1
                continue
            if t == "function_call":
                name = rec.get("name") or "unknown"
                raw_args = rec.get("arguments")
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                except Exception:
                    args = {"_raw": raw_args}
                call_id = rec.get("call_id") or rec.get("id") or ""
                pre = self.store.append(make_event(
                    session_id=session_id,
                    seq=self.store.next_seq(session_id),
                    type=EventType.PRE_TOOL_USE,
                    payload={"tool_name": name, "input": args},
                    ts=ts,
                ))
                if call_id:
                    open_calls[call_id] = pre.id
                    open_calls_meta[call_id] = {"name": name, "input": args}
                fifo_unpaired.append(call_id or f"__pos_{pre.id}")
                if not call_id:
                    open_calls_meta[f"__pos_{pre.id}"] = {"name": name, "input": args}
                    open_calls[f"__pos_{pre.id}"] = pre.id
                stats.tool_uses += 1
                stats.events_written += 1
                continue
            if t == "function_call_output":
                call_id = rec.get("call_id") or rec.get("id") or ""
                key = call_id if call_id in open_calls else (
                    fifo_unpaired.pop(0) if fifo_unpaired else ""
                )
                pre_id = open_calls.pop(key, "") if key else ""
                meta = open_calls_meta.pop(key, {}) if key else {}
                output = rec.get("output")
                success = not bool(rec.get("is_error"))
                post = self.store.append(make_event(
                    session_id=session_id,
                    seq=self.store.next_seq(session_id),
                    type=EventType.POST_TOOL_USE,
                    payload={
                        "tool_name": meta.get("name") or "unknown",
                        "output": _truncate(output),
                        "success": success,
                    },
                    parent_ids=(pre_id,) if pre_id else (),
                    ts=ts,
                ))
                stats.tool_results += 1
                stats.events_written += 1
                # File promotion
                self._maybe_promote_file(
                    session_id, meta.get("name") or "",
                    meta.get("input") or {}, output,
                    parent_id=post.id, stats=stats, ts=ts,
                )
                continue
            # session_meta and anything unknown: skipped
            stats.skipped += 1

    def _maybe_promote_file(
        self, session_id: str, tool_name: str, tool_input: dict,
        tool_output: object, *, parent_id: str, stats: ImportStats, ts: float,
    ) -> None:
        lower = tool_name.lower()
        path: str | None = None
        kind: EventType | None = None
        if lower in {"read_file", "read"}:
            path = tool_input.get("path") or tool_input.get("file_path")
            kind = EventType.FILE_READ
        elif lower in {"apply_patch", "write_file", "edit_file"}:
            path = tool_input.get("path") or tool_input.get("file_path")
            # apply_patch commonly encodes paths inside a patch body
            if not path and isinstance(tool_input.get("input"), str):
                m = re.search(r"\*\*\* (?:Add|Update|Delete) File: (.+)",
                              tool_input["input"])
                if m:
                    path = m.group(1).strip()
            kind = EventType.FILE_WRITE
        elif lower == "shell":
            # Best-effort: detect `cat path` or redirections.
            cmd = tool_input.get("command")
            if isinstance(cmd, list):
                cmd = " ".join(str(x) for x in cmd)
            if isinstance(cmd, str):
                m = re.search(r"(?:cat|less|head|tail)\s+(\S+)", cmd)
                if m:
                    path, kind = m.group(1), EventType.FILE_READ
                else:
                    m = re.search(r">\s*(\S+)", cmd)
                    if m:
                        path, kind = m.group(1), EventType.FILE_WRITE
        if not path or kind is None:
            return
        self.store.append(make_event(
            session_id=session_id,
            seq=self.store.next_seq(session_id),
            type=kind,
            payload={"path": path, "content_hash": "", "bytes": 0},
            parent_ids=(parent_id,),
            ts=ts,
        ))
        if kind is EventType.FILE_READ:
            stats.file_reads += 1
        else:
            stats.file_writes += 1
        stats.events_written += 1


# ---- helpers ----

def _ts(rec: dict) -> float:
    ts = rec.get("timestamp") or rec.get("ts") or rec.get("created_at")
    if not ts:
        return 0.0
    try:
        if isinstance(ts, (int, float)):
            return float(ts)
        return _dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                parts.append(b.get("text") or b.get("content") or "")
            elif isinstance(b, str):
                parts.append(b)
        return "\n".join(p for p in parts if p)
    return ""


def _truncate(v: object, n: int = 4096) -> object:
    if isinstance(v, str) and len(v) > n:
        return v[:n] + f"... [truncated {len(v) - n} chars]"
    return v
