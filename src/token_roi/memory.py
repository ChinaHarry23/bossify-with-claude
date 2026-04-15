"""Memory layer.

Two-tier structure:
    data/memory/MEMORY.md       — the index. Always small (< ~200 lines).
                                  One line per topic: "- [Title](topics/file.md) — hook"
    data/memory/topics/*.md     — individual compressed memories with frontmatter.

Each topic file has YAML-ish frontmatter + body:

    ---
    name: {name}
    description: {1-line description}
    type: {user|feedback|project|reference}
    source_events: [evt_id, evt_id, ...]
    created_at: {iso ts}
    updated_at: {iso ts}
    ---

    {body content}

The `source_events` field is critical: it is the audit back-reference that
lets the ROI system answer "which raw events contributed to this memory?"
Every field listed is derivable from raw events; this file is a *cache*.

This module is intentionally agnostic of WHO is writing to memory: the agent,
a compression pass, or a human. It just enforces the layout and emits
MEMORY_WRITE events so the event log can later reconstruct the trajectory.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from .events import EventType, make_event
from .storage import EventStore


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_INDEX_LINE_RE = re.compile(r"^- \[(?P<title>[^\]]+)\]\((?P<path>[^)]+)\)(?:\s+—\s+(?P<hook>.*))?$")


@dataclass
class MemoryEntry:
    """In-memory representation of one topic file."""
    name: str
    description: str
    type: str
    body: str
    source_events: list[str] = field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None
    path: Path | None = None   # filesystem location if already persisted

    def render(self) -> str:
        fm_lines = [
            "---",
            f"name: {self.name}",
            f"description: {self.description}",
            f"type: {self.type}",
            f"source_events: [{', '.join(self.source_events)}]",
        ]
        if self.created_at:
            fm_lines.append(f"created_at: {self.created_at}")
        if self.updated_at:
            fm_lines.append(f"updated_at: {self.updated_at}")
        fm_lines.append("---")
        return "\n".join(fm_lines) + "\n\n" + self.body.rstrip() + "\n"

    @staticmethod
    def parse(text: str, path: Path | None = None) -> "MemoryEntry":
        m = _FRONTMATTER_RE.match(text)
        if not m:
            raise ValueError(f"memory file {path} missing frontmatter")
        fm_text = m.group(1)
        body = text[m.end():]
        fm: dict[str, str] = {}
        for line in fm_text.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                fm[k.strip()] = v.strip()
        # source_events is a simple bracketed list; keep parsing forgiving.
        raw_sources = fm.get("source_events", "[]").strip()
        raw_sources = raw_sources.strip("[]")
        sources = [s.strip() for s in raw_sources.split(",") if s.strip()]
        return MemoryEntry(
            name=fm.get("name", path.stem if path else "unknown"),
            description=fm.get("description", ""),
            type=fm.get("type", "project"),
            body=body,
            source_events=sources,
            created_at=fm.get("created_at"),
            updated_at=fm.get("updated_at"),
            path=path,
        )


class MemoryLayer:
    """Filesystem-backed memory layer.

    Responsibilities:
        - Enforce the MEMORY.md + topics/ layout.
        - Emit MEMORY_READ / MEMORY_WRITE / MEMORY_DELETE events so every
          mutation is captured in the raw event log.
        - Provide the read/write surface that the compression engine uses.
    """

    def __init__(self, root: Path | str, store: EventStore | None = None):
        self.root = Path(root)
        self.topics_dir = self.root / "topics"
        self.index_path = self.root / "MEMORY.md"
        self.root.mkdir(parents=True, exist_ok=True)
        self.topics_dir.mkdir(parents=True, exist_ok=True)
        self._store = store

    def bind(self, store: EventStore) -> None:
        """Attach an event store after construction (used when the CLI wires
        things together in a fixed order)."""
        self._store = store

    # ---- reads ----

    def read_index(self, *, session_id: str | None = None) -> str:
        if not self.index_path.exists():
            return ""
        content = self.index_path.read_text(encoding="utf-8")
        self._emit_read(self.index_path, content, session_id)
        return content

    def list_topics(self) -> list[Path]:
        return sorted(self.topics_dir.glob("*.md"))

    def read_topic(self, name: str, *, session_id: str | None = None) -> MemoryEntry:
        path = self._topic_path(name)
        if not path.exists():
            raise FileNotFoundError(f"topic {name!r} not found at {path}")
        text = path.read_text(encoding="utf-8")
        self._emit_read(path, text, session_id)
        return MemoryEntry.parse(text, path=path)

    def iter_entries(self) -> Iterator[MemoryEntry]:
        for p in self.list_topics():
            try:
                yield MemoryEntry.parse(p.read_text(encoding="utf-8"), path=p)
            except ValueError:
                continue

    def index_lines(self) -> list[tuple[str, str, str]]:
        """Parse MEMORY.md into (title, path, hook) tuples."""
        content = self.index_path.read_text(encoding="utf-8") if self.index_path.exists() else ""
        out: list[tuple[str, str, str]] = []
        for line in content.splitlines():
            m = _INDEX_LINE_RE.match(line)
            if m:
                out.append((m.group("title"), m.group("path"), m.group("hook") or ""))
        return out

    # ---- writes ----

    def write_topic(self, entry: MemoryEntry, *, session_id: str | None = None) -> Path:
        """Write a topic file and emit a MEMORY_WRITE event.

        `entry.name` becomes the filename stem (sanitized).
        """
        now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        entry.updated_at = now
        if entry.created_at is None:
            entry.created_at = now
        path = self._topic_path(entry.name)
        body = entry.render()
        path.write_text(body, encoding="utf-8")
        entry.path = path
        self._emit_write(path, body, kind=entry.type, session_id=session_id)
        return path

    def update_index(self, entries: list[tuple[str, str, str]], *, session_id: str | None = None) -> None:
        """Rewrite MEMORY.md from a list of (title, relative_path, hook).

        Keep lines under ~150 chars each. The 200-line hard cap is enforced
        by truncating overflow.
        """
        lines: list[str] = []
        for title, path, hook in entries:
            suffix = f" — {hook}" if hook else ""
            line = f"- [{title}]({path}){suffix}"
            if len(line) > 200:
                line = line[:197] + "..."
            lines.append(line)
        lines = lines[:200]
        body = "\n".join(lines) + "\n"
        self.index_path.write_text(body, encoding="utf-8")
        self._emit_write(self.index_path, body, kind="index", session_id=session_id)

    def delete_topic(self, name: str, *, session_id: str | None = None) -> bool:
        path = self._topic_path(name)
        if not path.exists():
            return False
        path.unlink()
        if self._store and session_id:
            self._store.append(make_event(
                session_id=session_id,
                seq=self._store._next_seq(session_id),
                type=EventType.MEMORY_DELETE,
                payload={"path": str(path)},
            ))
        return True

    # ---- helpers ----

    def _topic_path(self, name: str) -> Path:
        safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", name).strip("._-") or "topic"
        if not safe.endswith(".md"):
            safe += ".md"
        return self.topics_dir / safe

    def _emit_read(self, path: Path, content: str, session_id: str | None) -> None:
        if self._store is None or session_id is None:
            return
        self._store.append(make_event(
            session_id=session_id,
            seq=self._store._next_seq(session_id),
            type=EventType.MEMORY_READ,
            payload={
                "path": str(path),
                "content_hash": _hash(content),
                "bytes": len(content),
            },
        ))

    def _emit_write(self, path: Path, content: str, *, kind: str, session_id: str | None) -> None:
        if self._store is None or session_id is None:
            return
        self._store.append(make_event(
            session_id=session_id,
            seq=self._store._next_seq(session_id),
            type=EventType.MEMORY_WRITE,
            payload={
                "path": str(path),
                "content_hash": _hash(content),
                "bytes": len(content),
                "kind": kind,
            },
        ))


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]
