"""Event schema.

Every byte of agent activity is modeled as a typed event. Events are
**immutable once written** and form a causal DAG via `parent_ids`.

Design invariants:
    - An event's `id` is a deterministic hash of (session_id, seq, type, payload_hash).
      This means: given the same raw stream, the same ids are produced on any
      machine. Replay is byte-stable.
    - `parent_ids` points backwards through the DAG. A PostToolUse points to the
      PreToolUse. An AssistantMessage points to the UserPrompt that triggered it.
      Memory writes point to the assistant message that authored them. Retrievals
      point to the prompt that spawned them.
    - `tokens_in` / `tokens_out` / `cached_tokens` / `cache_creation_tokens` are
      populated when the transport reports them (Anthropic usage block, hook
      payload, etc.). They are the *authoritative* cost signal — we do not
      estimate.
    - `outcome` is populated lazily by the attribution engine, not at write time.
      Raw events capture what happened, not whether it was good.

Event types intentionally stay flat (no polymorphic subclass hierarchy). The
payload is a free-form dict validated by `validate_payload`.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class EventType(str, Enum):
    # Conversation turn events
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    USER_PROMPT = "user_prompt"
    ASSISTANT_MESSAGE = "assistant_message"

    # Tool events (a PRE + POST pair per tool invocation)
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"
    TOOL_ERROR = "tool_error"

    # File I/O surfaced as distinct events so file touch volume is queryable
    # without parsing tool payloads.
    FILE_READ = "file_read"
    FILE_WRITE = "file_write"

    # Memory layer — note that MEMORY.md edits produced by the agent show up
    # as FILE_WRITE events *and* as typed MEMORY_WRITE events when the skill's
    # memory layer is the writer. The distinction matters for ROI.
    MEMORY_READ = "memory_read"
    MEMORY_WRITE = "memory_write"
    MEMORY_DELETE = "memory_delete"

    # Retrieval layer
    RETRIEVAL_QUERY = "retrieval_query"
    RETRIEVAL_RESULT = "retrieval_result"

    # Compression passes are themselves events — they are part of ground truth.
    COMPRESSION_RUN = "compression_run"

    # Outcome events (emitted by external tools: test pass/fail, git commit, etc.)
    OUTCOME = "outcome"

    # Attribution/ROI writes are NOT events — they are derived state in the DB.


# Payload keys treated as "significant" for content hashing. Everything else is
# metadata (latency, ids, timestamps) that should not affect event identity.
_CONTENT_KEYS: dict[EventType, tuple[str, ...]] = {
    EventType.USER_PROMPT: ("text",),
    EventType.ASSISTANT_MESSAGE: ("text",),
    EventType.PRE_TOOL_USE: ("tool_name", "input"),
    EventType.POST_TOOL_USE: ("tool_name", "output", "success"),
    EventType.TOOL_ERROR: ("tool_name", "error"),
    EventType.FILE_READ: ("path", "content_hash"),
    EventType.FILE_WRITE: ("path", "content_hash"),
    EventType.MEMORY_READ: ("path", "content_hash"),
    EventType.MEMORY_WRITE: ("path", "content_hash", "kind"),
    EventType.MEMORY_DELETE: ("path",),
    EventType.RETRIEVAL_QUERY: ("query",),
    EventType.RETRIEVAL_RESULT: ("query", "hits"),
    EventType.COMPRESSION_RUN: ("summary",),
    EventType.OUTCOME: ("kind", "detail"),
    EventType.SESSION_START: ("session_id",),
    EventType.SESSION_END: ("session_id",),
}


@dataclass(frozen=True)
class Event:
    """A single, immutable unit of agent activity."""

    id: str
    session_id: str
    seq: int                   # per-session monotonic
    ts: float                  # unix epoch seconds, float precision
    type: EventType
    payload: dict[str, Any]
    parent_ids: tuple[str, ...] = ()

    # Token accounting (zero-defaulted when N/A, e.g. for file reads).
    tokens_in: int = 0
    tokens_out: int = 0
    cached_tokens: int = 0
    cache_creation_tokens: int = 0

    # Optional transport-level metadata.
    model: str | None = None
    latency_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["type"] = self.type.value
        d["parent_ids"] = list(self.parent_ids)
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Event":
        return Event(
            id=d["id"],
            session_id=d["session_id"],
            seq=int(d["seq"]),
            ts=float(d["ts"]),
            type=EventType(d["type"]),
            payload=d.get("payload", {}) or {},
            parent_ids=tuple(d.get("parent_ids", []) or ()),
            tokens_in=int(d.get("tokens_in") or 0),
            tokens_out=int(d.get("tokens_out") or 0),
            cached_tokens=int(d.get("cached_tokens") or 0),
            cache_creation_tokens=int(d.get("cache_creation_tokens") or 0),
            model=d.get("model"),
            latency_ms=d.get("latency_ms"),
        )

    @staticmethod
    def from_json(s: str) -> "Event":
        return Event.from_dict(json.loads(s))

    # ---- derived properties ----

    @property
    def total_tokens(self) -> int:
        """Raw token flow — all in/out/cache/cache_creation summed.

        This is a *volume* metric. Useful for the dashboard's "total flow"
        KPI but a bad denominator for ROI because cache reads are cheap and
        would otherwise dwarf the signal. For ROI, use `effective_cost_tokens`.
        """
        return (
            self.tokens_in
            + self.tokens_out
            + self.cached_tokens
            + self.cache_creation_tokens
        )

    @property
    def effective_cost_tokens(self) -> int:
        """Billable-equivalent token cost, approximating Anthropic's pricing.

        Cache reads on Anthropic's API are priced at ~10% of a standard
        input token, so we discount them. Cache creation is priced at
        1.25x input but we treat it as 1x for simplicity — the ROI model
        is sensitive to order-of-magnitude, not pricing precision.

        Using this as the denominator in ROI scoring keeps cache-dominated
        Claude Code sessions from drowning their own signal: a 30M-token
        flow that is 29M cached reads produces an effective cost of only
        ~1M equivalent tokens.
        """
        return (
            self.tokens_in
            + self.tokens_out
            + self.cache_creation_tokens
            + self.cached_tokens // 10   # integer floor is fine; < 0.01 tok error
        )


def _payload_hash(event_type: EventType, payload: dict[str, Any]) -> str:
    """Hash of the content-significant subset of the payload.

    We deliberately ignore metadata like `pid`, `path_absolute`, and anything
    timestamp-shaped. If a new event type is added without an entry in
    `_CONTENT_KEYS`, we fall back to hashing the whole payload — safe but
    means identity is noisier.
    """
    keys = _CONTENT_KEYS.get(event_type)
    if keys is None:
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    else:
        subset = {k: payload.get(k) for k in keys}
        canonical = json.dumps(subset, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _event_id(session_id: str, seq: int, type_: EventType, payload: dict[str, Any]) -> str:
    """Deterministic event id.

    Components:
        - session_id: scopes replay
        - seq: preserves order even for identical payloads (e.g. two identical
               retrieval queries issued in sequence)
        - type + payload_hash: content identity

    Collisions within a session at different seqs are essentially impossible
    with 16 hex chars of content hash + 8 hex chars of seq+session mixing.
    """
    mix = f"{session_id}:{seq}:{type_.value}:{_payload_hash(type_, payload)}"
    return hashlib.sha256(mix.encode("utf-8")).hexdigest()[:24]


def make_event(
    *,
    session_id: str,
    seq: int,
    type: EventType,
    payload: dict[str, Any] | None = None,
    parent_ids: tuple[str, ...] | list[str] = (),
    tokens_in: int = 0,
    tokens_out: int = 0,
    cached_tokens: int = 0,
    cache_creation_tokens: int = 0,
    model: str | None = None,
    latency_ms: int | None = None,
    ts: float | None = None,
) -> Event:
    """Construct a well-formed Event with a deterministic id.

    This is the only blessed constructor — writing `Event(...)` directly works
    but you are responsible for your own id.
    """
    payload = payload or {}
    validate_payload(type, payload)
    return Event(
        id=_event_id(session_id, seq, type, payload),
        session_id=session_id,
        seq=seq,
        ts=ts if ts is not None else time.time(),
        type=type,
        payload=payload,
        parent_ids=tuple(parent_ids),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cached_tokens=cached_tokens,
        cache_creation_tokens=cache_creation_tokens,
        model=model,
        latency_ms=latency_ms,
    )


def new_session_id() -> str:
    """Generate a session id. Non-deterministic — this is the one place where
    randomness is acceptable because a session must be globally unique across
    machines."""
    return uuid.uuid4().hex[:16]


# ---- Payload validation ----

class PayloadError(ValueError):
    pass


_REQUIRED_FIELDS: dict[EventType, tuple[str, ...]] = {
    EventType.USER_PROMPT: ("text",),
    EventType.ASSISTANT_MESSAGE: ("text",),
    EventType.PRE_TOOL_USE: ("tool_name", "input"),
    EventType.POST_TOOL_USE: ("tool_name", "success"),
    EventType.TOOL_ERROR: ("tool_name", "error"),
    EventType.FILE_READ: ("path",),
    EventType.FILE_WRITE: ("path",),
    EventType.MEMORY_READ: ("path",),
    EventType.MEMORY_WRITE: ("path", "kind"),
    EventType.MEMORY_DELETE: ("path",),
    EventType.RETRIEVAL_QUERY: ("query",),
    EventType.RETRIEVAL_RESULT: ("query", "hits"),
    EventType.COMPRESSION_RUN: ("summary",),
    EventType.OUTCOME: ("kind",),
    EventType.SESSION_START: (),
    EventType.SESSION_END: (),
}


def validate_payload(type_: EventType, payload: dict[str, Any]) -> None:
    """Raise PayloadError if required keys for a type are missing."""
    required = _REQUIRED_FIELDS.get(type_, ())
    missing = [k for k in required if k not in payload]
    if missing:
        raise PayloadError(
            f"Event type {type_.value!r} missing required payload keys: {missing}"
        )


# ---- synthetic prompt detection -----------------------------------------
#
# Claude Code's JSONL history interleaves real user prompts with a handful
# of synthetic ones that look like user turns but aren't:
#   - <local-command-caveat> / <local-command-stdout> / <local-command-stderr>
#   - <command-name>/<command-message> (slash-command wrappers)
#   - <task-notification> (subagent task plumbing)
#   - "This session is being continued from a previous conversation…"
#     (auto-injected after context compaction)
#
# These have zero intent signal and (often) zero billable cost, but the
# pipeline happily judges them and counts them as WASTED. That pollutes
# the ROI distribution. We detect them once here and let every downstream
# consumer filter consistently.
#
# The detector is intentionally conservative — we'd rather judge a weird
# real prompt than drop one.

_SYNTHETIC_PROMPT_PREFIXES = (
    "<local-command-",
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<task-notification>",
    "<local-command-caveat>",
    "<local-command-stdout>",
    "<local-command-stderr>",
)

_SYNTHETIC_PROMPT_INFIXES = (
    "This session is being continued from a previous conversation",
    "Caveat: The messages below were generated by the",
)


def is_synthetic_prompt(text: str | None) -> bool:
    """Return True for non-intent prompt text emitted by Claude Code plumbing.

    Blank and whitespace-only prompts also count as synthetic because they
    carry no user intent even when they are structurally a USER_PROMPT.
    """
    if not text:
        return True
    stripped = text.lstrip()
    if not stripped:
        return True
    if any(stripped.startswith(p) for p in _SYNTHETIC_PROMPT_PREFIXES):
        return True
    head = text[:400]
    return any(sig in head for sig in _SYNTHETIC_PROMPT_INFIXES)
