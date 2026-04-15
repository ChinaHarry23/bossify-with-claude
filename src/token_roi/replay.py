"""Deterministic session replay.

Replay is the audit primitive: given a session id, reconstruct the complete
timeline and print / render it so the user can verify that every ROI score
is traceable to real events.

Replay reads from `raw_events/` (the ground truth), NOT from the DB. This
way if the DB is corrupt, replay still works.

Two output modes:
    - `text` (default): human-readable timeline, one line per event.
    - `jsonl`: one event per line, suitable for piping into analysis tools.

Seek:
    `--from <event_id>` starts at a specific event. Handy when chasing a
    single prompt's downstream activity.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, TextIO

from .events import Event, EventType
from .storage import EventStore


@dataclass
class ReplayOptions:
    mode: str = "text"       # text | jsonl
    start_event: str | None = None
    show_payload: bool = False


class Replayer:
    def __init__(self, store: EventStore):
        self.store = store

    def replay_session(
        self,
        session_id: str,
        *,
        opts: ReplayOptions | None = None,
        out: TextIO | None = None,
    ) -> int:
        opts = opts or ReplayOptions()
        out = out or sys.stdout
        count = 0
        started = opts.start_event is None
        for ev in self.store.iter_session(session_id):
            if not started:
                if ev.id == opts.start_event:
                    started = True
                else:
                    continue
            self._emit(ev, opts=opts, out=out)
            count += 1
        return count

    # ---- emitters ----

    def _emit(self, ev: Event, *, opts: ReplayOptions, out: TextIO) -> None:
        if opts.mode == "jsonl":
            out.write(ev.to_json() + "\n")
            return
        header = f"[{ev.seq:>5}] {_fmt_ts(ev.ts)} {ev.type.value:<22} id={ev.id[:10]}"
        if ev.tokens_in or ev.tokens_out:
            header += (f"  tokens=in:{ev.tokens_in} out:{ev.tokens_out}"
                       f" cached:{ev.cached_tokens}")
        if ev.latency_ms is not None:
            header += f"  latency={ev.latency_ms}ms"
        out.write(header + "\n")
        if opts.show_payload:
            payload = _format_payload(ev)
            for line in payload.splitlines():
                out.write("        " + line + "\n")


def _fmt_ts(ts: float) -> str:
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%H:%M:%S.%f")[:-3]


def _format_payload(ev: Event) -> str:
    if ev.type is EventType.USER_PROMPT or ev.type is EventType.ASSISTANT_MESSAGE:
        txt = ev.payload.get("text", "")
        return _truncate(txt, 400)
    if ev.type is EventType.POST_TOOL_USE or ev.type is EventType.PRE_TOOL_USE:
        return json.dumps({
            "tool": ev.payload.get("tool_name"),
            "input_keys": list((ev.payload.get("input") or {}).keys()),
        })
    if ev.type is EventType.RETRIEVAL_RESULT:
        hits = ev.payload.get("hits") or []
        titles = [h.get("title") for h in hits[:5]]
        return f"query={ev.payload.get('query')!r} hits={titles}"
    if ev.type is EventType.MEMORY_WRITE:
        return f"path={ev.payload.get('path')} bytes={ev.payload.get('bytes')}"
    return _truncate(json.dumps(ev.payload, ensure_ascii=False), 400)


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + "..."
