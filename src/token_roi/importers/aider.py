"""Import Aider session history.

Aider (github.com/Aider-AI/aider) writes two files per project:

    .aider.chat.history.md   — markdown transcript; `#### ` separates user
                               prompts from assistant responses. Lines
                               starting with `> ` inside a user section are
                               citation context (files added to chat), not
                               direct user input, but we keep them.
    .aider.llm.history       — line-based log with "TO LLM" / "FROM LLM"
                               blocks including token counts and model.

We use the markdown transcript as the primary source of events and, when
``.aider.llm.history`` is present in the same directory, enrich the
ASSISTANT_MESSAGE events with tokens_in/out and model by matching in
chronological order.

Fenced code blocks inside assistant turns whose info-string looks like a
file path are treated as FILE_WRITE events.
"""
from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

from ..events import EventType, is_synthetic_prompt, make_event
from . import ImportStats, Importer, register

log = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^```([^\s`]+)?\s*$")
_HEADER_RE = re.compile(r"^####\s+(.*)$")


@register
class AiderImporter(Importer):
    source_name = "aider"

    @classmethod
    def default_path(cls) -> Path:
        return Path.cwd()

    def import_path(
        self, path: Path | str, *, project_filter: str | None = None
    ) -> ImportStats:
        p = Path(path).expanduser()
        stats = ImportStats()
        if p.is_file() and p.name == ".aider.chat.history.md":
            files = [p]
        elif p.is_dir():
            files = sorted(p.rglob(".aider.chat.history.md"))
        else:
            raise FileNotFoundError(f"no aider chat history found at {p}")
        for f in files:
            if project_filter and project_filter not in str(f):
                continue
            self._import_file(f, stats)
        return stats

    def _import_file(self, path: Path, stats: ImportStats) -> None:
        stats.files += 1
        project_path = path.parent
        project_slug = project_path.name or "aider"

        # session id is deterministic from (project_path, first mtime)
        first_mtime = int(path.stat().st_mtime)
        sid_src = f"{project_path}|{first_mtime}".encode()
        session_id = "aider-" + hashlib.sha256(sid_src).hexdigest()[:16]
        self.store.start_session(session_id)

        employee_id = None
        if self.employees is not None:
            employee_id = self.employees.resolve_for_slug(project_slug).id
        if self.db is not None:
            self.db.upsert_session_metadata(
                session_id, project_slug=project_slug, employee_id=employee_id,
            )

        # Parse markdown into turns.
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        stats.lines += len(lines)
        turns = _split_turns(lines)

        # Load llm history enrichment (chronological list of usage dicts).
        llm_path = project_path / ".aider.llm.history"
        usages = _parse_llm_history(llm_path) if llm_path.exists() else []
        usage_iter = iter(usages)

        for kind, text in turns:
            if kind == "user":
                if is_synthetic_prompt(text):
                    stats.synthetic_prompts_dropped += 1
                    continue
                self.store.append(make_event(
                    session_id=session_id,
                    seq=self.store.next_seq(session_id),
                    type=EventType.USER_PROMPT,
                    payload={"text": text},
                ))
                stats.user_prompts += 1
                stats.events_written += 1
            elif kind == "assistant":
                usage = next(usage_iter, {})
                asst = self.store.append(make_event(
                    session_id=session_id,
                    seq=self.store.next_seq(session_id),
                    type=EventType.ASSISTANT_MESSAGE,
                    payload={"text": text},
                    tokens_in=int(usage.get("tokens_in") or 0),
                    tokens_out=int(usage.get("tokens_out") or 0),
                    model=usage.get("model"),
                ))
                stats.assistant_messages += 1
                stats.events_written += 1
                # Emit FILE_WRITE for each fenced code block with a path.
                for path_hint, body in _extract_code_blocks(text):
                    h = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
                    self.store.append(make_event(
                        session_id=session_id,
                        seq=self.store.next_seq(session_id),
                        type=EventType.FILE_WRITE,
                        payload={
                            "path": path_hint,
                            "content_hash": h,
                            "bytes": len(body.encode("utf-8")),
                        },
                        parent_ids=(asst.id,),
                    ))
                    stats.file_writes += 1
                    stats.events_written += 1
            else:
                stats.skipped += 1


# ---- helpers ----

def _split_turns(lines: list[str]) -> list[tuple[str, str]]:
    """Return an ordered list of (kind, text) tuples.

    kind is "user" for sections introduced by `####` (those typically
    quote aider's prompt + user input) and "assistant" for free text
    between two `####` headers.
    """
    turns: list[tuple[str, str]] = []
    buf: list[str] = []
    current = "assistant"  # text before any header is from aider
    for ln in lines:
        m = _HEADER_RE.match(ln)
        if m:
            if buf:
                turns.append((current, "\n".join(buf).strip()))
                buf = []
            # The `#### ` header text is the user prompt itself.
            buf.append(m.group(1))
            current = "user"
            continue
        # after user header, next blank-line-separated text switches back
        # to assistant once we've consumed at least one body line.
        if current == "user" and ln.strip() == "" and buf and buf[-1] != "":
            # flush user turn
            turns.append(("user", "\n".join(buf).strip()))
            buf = []
            current = "assistant"
            continue
        buf.append(ln)
    if buf:
        turns.append((current, "\n".join(buf).strip()))
    # drop empty
    return [(k, t) for k, t in turns if t]


def _extract_code_blocks(text: str) -> list[tuple[str, str]]:
    """Return [(path, body), ...] for fenced blocks whose info string
    looks like a file path (contains a dot or a slash)."""
    out: list[tuple[str, str]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = _FENCE_RE.match(lines[i])
        if m and m.group(1) and ("." in m.group(1) or "/" in m.group(1)):
            info = m.group(1)
            body_lines = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                body_lines.append(lines[i])
                i += 1
            out.append((info, "\n".join(body_lines)))
        i += 1
    return out


def _parse_llm_history(path: Path) -> list[dict]:
    """Parse .aider.llm.history into a list of {tokens_in, tokens_out, model}
    dicts in chronological order, one per assistant response.
    """
    out: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return out
    current: dict = {}
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("FROM LLM"):
            if current:
                out.append(current)
            current = {}
            m = re.search(r"model=([\w\-./:]+)", s)
            if m:
                current["model"] = m.group(1)
        elif s.startswith("TO LLM"):
            if current:
                out.append(current)
                current = {}
        else:
            m = re.search(r"prompt_tokens[:=\s]+(\d+)", s)
            if m:
                current["tokens_in"] = int(m.group(1))
            m = re.search(r"completion_tokens[:=\s]+(\d+)", s)
            if m:
                current["tokens_out"] = int(m.group(1))
            m = re.search(r"model[:=\s]+([\w\-./:]+)", s)
            if m and "model" not in current:
                current["model"] = m.group(1)
    if current:
        out.append(current)
    # Only return entries that had at least one field populated.
    return [u for u in out if u]
