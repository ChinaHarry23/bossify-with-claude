"""Generic importer for OpenAI Responses API / Chat Completions JSONL logs.

Each line is expected to be a JSON object shaped roughly like::

    {
      "model": "gpt-4.1",
      "created_at": 1700000000,
      "input":  [ {"role": "user", "content": "..."}, ... ],
      "output": [ {"type": "message", "role": "assistant",
                   "content": [{"type": "output_text", "text": "..."}]},
                  {"type": "function_call", "name": "...", "arguments": "..."} ],
      "usage":  {"input_tokens": N, "output_tokens": N, "cached_input_tokens": N}
    }

Chat Completions payloads (``messages``/``choices``) are accepted as a
fallback. Only the *last* user message in ``input`` is emitted as a
USER_PROMPT to avoid double-counting multi-turn history on subsequent
lines.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from ..events import EventType, is_synthetic_prompt, make_event
from . import ImportStats, Importer, register

log = logging.getLogger(__name__)


@register
class OpenAIJsonlImporter(Importer):
    source_name = "openai-jsonl"

    @classmethod
    def default_path(cls) -> Path:
        return Path.cwd()

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
            raise FileNotFoundError(f"no OpenAI JSONL found at {p}")
        for f in files:
            if project_filter and project_filter not in str(f):
                continue
            self._import_file(f, stats)
        return stats

    def _import_file(self, path: Path, stats: ImportStats) -> None:
        stats.files += 1
        session_id = path.stem
        self.store.start_session(session_id)
        project_slug = path.parent.name or "openai-jsonl"

        employee_id = None
        if self.employees is not None:
            employee_id = self.employees.resolve_for_slug(project_slug).id
        if self.db is not None:
            self.db.upsert_session_metadata(
                session_id,
                project_slug=project_slug,
                employee_id=employee_id,
                platform=self.source_name,
            )

        # Track open function_calls by call_id so subsequent lines'
        # function_call_output input blocks can be paired.
        open_calls: dict[str, tuple[str, str]] = {}  # call_id -> (pre_id, name)

        for pos, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines()
        ):
            stats.lines += 1
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                stats.skipped += 1
                continue
            try:
                self._emit_record(session_id, rec, open_calls, stats)
            except Exception as e:  # defensive
                log.warning("skip record %s:%d: %s", path, pos, e)
                stats.skipped += 1

    def _emit_record(
        self, session_id: str, rec: dict,
        open_calls: dict[str, tuple[str, str]], stats: ImportStats,
    ) -> None:
        ts = float(rec.get("created_at") or rec.get("created") or 0.0)
        model = rec.get("model")
        usage = rec.get("usage") or {}

        # --- input side: last user message + function_call_outputs ---
        inp = rec.get("input")
        if inp is None and "messages" in rec:
            inp = rec.get("messages")

        last_user_text: str | None = None
        if isinstance(inp, list):
            for block in inp:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "function_call_output":
                    call_id = block.get("call_id") or block.get("id") or ""
                    pre_id, name = open_calls.pop(call_id, ("", "unknown"))
                    self.store.append(make_event(
                        session_id=session_id,
                        seq=self.store.next_seq(session_id),
                        type=EventType.POST_TOOL_USE,
                        payload={
                            "tool_name": name,
                            "output": block.get("output"),
                            "success": not bool(block.get("is_error")),
                        },
                        parent_ids=(pre_id,) if pre_id else (),
                        ts=ts,
                    ))
                    stats.tool_results += 1
                    stats.events_written += 1
                    continue
                role = block.get("role")
                if role == "user":
                    last_user_text = _text_of(block.get("content"))

        if last_user_text:
            if is_synthetic_prompt(last_user_text):
                stats.synthetic_prompts_dropped += 1
            else:
                self.store.append(make_event(
                    session_id=session_id,
                    seq=self.store.next_seq(session_id),
                    type=EventType.USER_PROMPT,
                    payload={"text": last_user_text},
                    ts=ts,
                ))
                stats.user_prompts += 1
                stats.events_written += 1

        # --- output side: assistant message + function_calls ---
        out = rec.get("output")
        if out is None and "choices" in rec:
            # Chat Completions format
            choices = rec.get("choices") or []
            out = []
            for ch in choices:
                m = (ch or {}).get("message") or {}
                out.append({
                    "type": "message",
                    "role": m.get("role", "assistant"),
                    "content": m.get("content"),
                })
                for tc in m.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    out.append({
                        "type": "function_call",
                        "call_id": tc.get("id"),
                        "name": fn.get("name"),
                        "arguments": fn.get("arguments"),
                    })

        assistant_text_parts: list[str] = []
        tool_calls: list[dict] = []
        if isinstance(out, list):
            for block in out:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "message" and block.get("role") != "user":
                    assistant_text_parts.append(_text_of(block.get("content")))
                elif btype == "function_call":
                    tool_calls.append(block)
                elif btype == "output_text":
                    assistant_text_parts.append(block.get("text") or "")

        text = "\n".join(t for t in assistant_text_parts if t)
        asst_id = ""
        if text or tool_calls or usage:
            ev = self.store.append(make_event(
                session_id=session_id,
                seq=self.store.next_seq(session_id),
                type=EventType.ASSISTANT_MESSAGE,
                payload={"text": text},
                tokens_in=int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
                tokens_out=int(usage.get("output_tokens") or usage.get("completion_tokens") or 0),
                cached_tokens=int(
                    usage.get("cached_input_tokens")
                    or (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
                    or 0
                ),
                model=model,
                ts=ts,
            ))
            asst_id = ev.id
            stats.assistant_messages += 1
            stats.events_written += 1

        for tc in tool_calls:
            name = tc.get("name") or "unknown"
            raw_args = tc.get("arguments")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
            except Exception:
                args = {"_raw": raw_args}
            pre = self.store.append(make_event(
                session_id=session_id,
                seq=self.store.next_seq(session_id),
                type=EventType.PRE_TOOL_USE,
                payload={"tool_name": name, "input": args},
                parent_ids=(asst_id,) if asst_id else (),
                ts=ts,
            ))
            call_id = tc.get("call_id") or tc.get("id")
            if call_id:
                open_calls[call_id] = (pre.id, name)
            stats.tool_uses += 1
            stats.events_written += 1


def _text_of(content: object) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                parts.append(b.get("text") or b.get("content") or "")
        return "\n".join(p for p in parts if p)
    return ""
