"""Append-only JSONL event storage.

This is the skill's canonical ground truth. It is **never** mutated in place.
Compaction is a separate offline operation that writes new files and leaves
the originals intact.

File layout:

    data/raw_events/
        YYYY-MM-DD/
            session_<id>.jsonl

One file per session per day. A session that crosses midnight rolls to a new
file; the session_id stays the same, so queries that aggregate by session
must glob across days.

Concurrency model:
    - Writes are line-buffered and use O_APPEND, which is atomic for single
      lines under POSIX up to PIPE_BUF (4096 bytes). Events larger than 4KB
      get a `write` + `fsync` with an advisory lockfile to serialize.
    - Multiple processes may write to the same session file concurrently
      (e.g. hook script + SDK wrapper in the same harness). `EventStore`
      uses `fcntl.flock` in POSIX to guarantee ordering.
    - Reads are purely sequential and safe without locking.

This is enough for the local-first use case. A multi-tenant server would
want something sturdier (e.g. postgres with WAL).
"""
from __future__ import annotations

import datetime as _dt
import fcntl
import gzip
import io
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from .events import Event, EventType, make_event, new_session_id


@dataclass
class StoragePaths:
    root: Path

    @property
    def raw_events(self) -> Path:
        return self.root / "raw_events"

    @property
    def snapshots(self) -> Path:
        return self.root / "snapshots"

    def day_dir(self, ts: float) -> Path:
        d = _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%Y-%m-%d")
        return self.raw_events / d

    def session_file(self, ts: float, session_id: str) -> Path:
        return self.day_dir(ts) / f"session_{session_id}.jsonl"


class EventStore:
    """Append-only store. Construct once per process; safe to keep around.

    Example
    -------
    >>> store = EventStore(Path("./data"))
    >>> sid = store.start_session()
    >>> ev = store.append_user_prompt(sid, "fix the failing test")
    >>> for e in store.iter_session(sid):
    ...     print(e.type, e.seq)
    """

    def __init__(self, data_dir: Path | str):
        self.paths = StoragePaths(root=Path(data_dir))
        self.paths.raw_events.mkdir(parents=True, exist_ok=True)
        self.paths.snapshots.mkdir(parents=True, exist_ok=True)
        # Per-session seq cache. Reloaded lazily from disk on demand.
        self._seq: dict[str, int] = {}

    @property
    def malformed_events_skipped(self) -> int:
        """Process-wide count of malformed JSONL lines seen by any reader."""
        return _MALFORMED_EVENTS_SEEN["count"]

    # ---- session lifecycle ----

    def start_session(self, session_id: str | None = None) -> str:
        """Open a new session and emit a SESSION_START event.

        Returns the session id. Idempotent: if `session_id` is passed and a
        session file already exists, this is a resume, not a start — we do
        *not* emit a duplicate SESSION_START.
        """
        sid = session_id or new_session_id()
        existing = self._locate_session_file(sid)
        if existing is not None:
            self._seq[sid] = self._max_seq_in_file(existing) + 1
            return sid
        self._seq[sid] = 0
        self.append(make_event(
            session_id=sid,
            seq=0,
            type=EventType.SESSION_START,
            payload={"session_id": sid},
        ))
        return sid

    def end_session(self, session_id: str) -> Event:
        return self.append(make_event(
            session_id=session_id,
            seq=self.next_seq(session_id),
            type=EventType.SESSION_END,
            payload={"session_id": session_id},
        ))

    # ---- append ----

    def append(self, event: Event) -> Event:
        """Append an event to its session's JSONL file.

        Returns the event unchanged; the return is for fluent call chains.
        """
        path = self.paths.session_file(event.ts, event.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = event.to_json() + "\n"

        # O_APPEND + advisory lock: guarantees atomic append-per-line even
        # when multiple processes share the file (hook + SDK wrapper).
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            os.write(fd, line.encode("utf-8"))
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

        # Keep seq cache in sync. It's fine if this is off by one on race —
        # the next `start_session` rebuild fixes it.
        self._seq[event.session_id] = max(self._seq.get(event.session_id, -1), event.seq) + 1
        return event

    # ---- convenience appenders (the 80% path) ----

    def append_user_prompt(self, sid: str, text: str, *, parent_ids: tuple[str, ...] = ()) -> Event:
        return self.append(make_event(
            session_id=sid, seq=self.next_seq(sid),
            type=EventType.USER_PROMPT, payload={"text": text},
            parent_ids=parent_ids,
        ))

    def append_assistant_message(
        self,
        sid: str,
        text: str,
        *,
        parent_ids: tuple[str, ...] = (),
        tokens_in: int = 0,
        tokens_out: int = 0,
        cached_tokens: int = 0,
        cache_creation_tokens: int = 0,
        model: str | None = None,
        latency_ms: int | None = None,
    ) -> Event:
        return self.append(make_event(
            session_id=sid, seq=self.next_seq(sid),
            type=EventType.ASSISTANT_MESSAGE, payload={"text": text},
            parent_ids=parent_ids,
            tokens_in=tokens_in, tokens_out=tokens_out,
            cached_tokens=cached_tokens,
            cache_creation_tokens=cache_creation_tokens,
            model=model, latency_ms=latency_ms,
        ))

    def append_tool_use(
        self,
        sid: str,
        *,
        tool_name: str,
        input_: dict,
        output: object = None,
        success: bool = True,
        parent_ids: tuple[str, ...] = (),
        latency_ms: int | None = None,
    ) -> tuple[Event, Event]:
        """Emit the PRE + POST pair in a single call.

        Some transports only expose the completed tool call, in which case
        both events have the same timestamp. That is fine — downstream
        analysis treats the pair as one atomic action.
        """
        pre = self.append(make_event(
            session_id=sid, seq=self.next_seq(sid),
            type=EventType.PRE_TOOL_USE,
            payload={"tool_name": tool_name, "input": input_},
            parent_ids=parent_ids,
        ))
        post = self.append(make_event(
            session_id=sid, seq=self.next_seq(sid),
            type=EventType.POST_TOOL_USE,
            payload={"tool_name": tool_name, "output": output, "success": success},
            parent_ids=(pre.id,),
            latency_ms=latency_ms,
        ))
        return pre, post

    # ---- reads ----

    def iter_session(self, session_id: str) -> Iterator[Event]:
        """Yield every event for a session in seq order.

        Scans every day-dir because a session can span midnight. This is O(n)
        over raw files — fine for local use. For the analytics hot path,
        query the SQLite index instead.
        """
        paths = sorted(self._session_files(session_id))
        events: list[Event] = []
        for p in paths:
            events.extend(_read_jsonl(p))
        events.sort(key=lambda e: e.seq)
        yield from events

    def iter_all_sessions(self, *, since_ts: float | None = None) -> Iterator[Event]:
        """Stream every event across every session, optionally filtered by ts.

        Used by the indexer's full-rebuild path.
        """
        for day_dir in sorted(self.paths.raw_events.iterdir()):
            if not day_dir.is_dir():
                continue
            for f in sorted(day_dir.glob("session_*.jsonl")):
                for ev in _read_jsonl(f):
                    if since_ts is not None and ev.ts < since_ts:
                        continue
                    yield ev

    def list_sessions(self) -> list[str]:
        """Return all session ids that have at least one event file.

        Sessions are keyed by id across all day-dirs.
        """
        ids: set[str] = set()
        for day_dir in self.paths.raw_events.iterdir():
            if not day_dir.is_dir():
                continue
            for f in day_dir.glob("session_*.jsonl"):
                # strip prefix "session_" and suffix ".jsonl"
                ids.add(f.stem[len("session_"):])
        return sorted(ids)

    # ---- snapshots (manual durability / backup) ----

    def snapshot(self) -> Path:
        """Gzip a copy of the raw event tree into snapshots/.

        Cheap insurance. Call before a compression pass or destructive
        analytics rebuild.
        """
        ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out = self.paths.snapshots / f"raw_events-{ts}.tar.gz"
        import tarfile
        with tarfile.open(out, "w:gz") as tar:
            tar.add(self.paths.raw_events, arcname="raw_events")
        return out

    # ---- internals ----

    def next_seq(self, session_id: str) -> int:
        """Allocate the next per-session sequence number.

        Lazily rebuilt from disk on first access for a given session.
        """
        if session_id not in self._seq:
            existing = self._locate_session_file(session_id)
            self._seq[session_id] = 0 if existing is None else self._max_seq_in_file(existing) + 1
        s = self._seq[session_id]
        self._seq[session_id] = s + 1
        return s

    # Legacy alias. Internal callers used ``_next_seq`` before the method
    # was promoted; keep it resolving so historical test fixtures and
    # any third-party callers don't break.
    _next_seq = next_seq

    def _session_files(self, session_id: str) -> list[Path]:
        hits: list[Path] = []
        if not self.paths.raw_events.exists():
            return hits
        for day_dir in self.paths.raw_events.iterdir():
            if not day_dir.is_dir():
                continue
            f = day_dir / f"session_{session_id}.jsonl"
            if f.exists():
                hits.append(f)
        return hits

    def _locate_session_file(self, session_id: str) -> Path | None:
        files = self._session_files(session_id)
        return files[-1] if files else None

    @staticmethod
    def _max_seq_in_file(path: Path) -> int:
        mx = -1
        for ev in _read_jsonl(path):
            if ev.seq > mx:
                mx = ev.seq
        return mx


# Process-wide tally of malformed JSONL lines seen by any reader in this
# process. EventStore.malformed_events_skipped returns this count so the
# dashboard can surface audit-trail gaps instead of letting them stay silent.
_MALFORMED_EVENTS_SEEN: dict[str, int] = {"count": 0}


def _read_jsonl(path: Path) -> Iterable[Event]:
    """Read a JSONL file, tolerating trailing truncation.

    A crash mid-write can leave a half line at the end; we log and skip it
    rather than abort, because the rest of the file is still ground truth.
    Every skip bumps the process-wide counter so a human can notice.
    """
    if not path.exists():
        return
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as f:  # type: ignore[arg-type]
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield Event.from_json(line)
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                # Advance past broken line; subsequent lines are still valid.
                _MALFORMED_EVENTS_SEEN["count"] += 1
                import logging
                logging.getLogger(__name__).error(
                    "skipping malformed event line %s:%d (%s): %r",
                    path, lineno, e, line[:200],
                )
                continue


@contextmanager
def session(store: EventStore, session_id: str | None = None):
    """Context manager that emits SESSION_START / SESSION_END around a block.

    Example:
        with session(store) as sid:
            store.append_user_prompt(sid, "...")
    """
    sid = store.start_session(session_id)
    try:
        yield sid
    finally:
        store.end_session(sid)
