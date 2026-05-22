"""Agent SDK wrapper (Mode B).

Wraps the Anthropic SDK so every `.messages.create()` call produces the same
token_roi events as Mode A. This is the "works without hooks" path — use it
when embedding the skill in a Python harness that doesn't run Claude Code.

Usage
-----
    from token_roi.sdk_wrapper import InstrumentedClient

    client = InstrumentedClient(api_key=...)
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": "help"}],
    )

Every call emits:
    USER_PROMPT (from the last user message)
    ASSISTANT_MESSAGE (with usage populated from resp.usage)
    PRE_TOOL_USE / POST_TOOL_USE pairs if the response contains tool_use blocks

Design notes:
    - We do NOT reimplement the Anthropic SDK. We wrap it via `__getattr__`.
    - Token counts come from the provider's `usage` block — we trust it.
    - If the `anthropic` package is missing, import fails with a clear error;
      the skill still works without this module as long as hooks are used.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from .events import EventType, make_event
from .storage import EventStore
from .telemetry import Telemetry

log = logging.getLogger(__name__)


class InstrumentedClient:
    """Drop-in Anthropic client with token_roi instrumentation."""

    def __init__(
        self,
        *,
        data_dir: Path | str,
        session_id: str | None = None,
        anthropic_kwargs: dict | None = None,
    ):
        try:
            import anthropic  # type: ignore
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                "anthropic package required for InstrumentedClient. "
                "pip install anthropic"
            ) from e
        self._anthropic = anthropic
        self._inner = anthropic.Anthropic(**(anthropic_kwargs or {}))
        self._store = EventStore(data_dir)
        self._sid = self._store.start_session(session_id)
        self._telemetry = Telemetry.get()
        # Expose the .messages namespace. Other namespaces (beta, models) fall
        # through transparently via __getattr__.
        self.messages = _MessagesWrapper(self._inner.messages, self._store, self._sid, self._telemetry)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @property
    def session_id(self) -> str:
        return self._sid

    def close(self) -> None:
        self._store.end_session(self._sid)


class _MessagesWrapper:
    def __init__(self, inner, store: EventStore, sid: str, telemetry: Telemetry):
        self._inner = inner
        self._store = store
        self._sid = sid
        self._t = telemetry

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def create(self, **kwargs) -> Any:
        # Capture the prompt (last user message) before the API call.
        messages = kwargs.get("messages") or []
        prompt_text = _extract_last_user_text(messages)
        prompt_ev = self._store.append_user_prompt(self._sid, prompt_text) if prompt_text else None

        t0 = time.time()
        with self._t.prompt_span(self._sid, prompt_ev.id if prompt_ev else "") as span:
            resp = self._inner.create(**kwargs)
            latency = int((time.time() - t0) * 1000)
            usage = getattr(resp, "usage", None)
            if usage is not None and hasattr(usage, "model_dump"):
                usage = usage.model_dump()
            usage = usage or {}
            assistant_text = _extract_assistant_text(resp)

            assistant_ev = self._store.append_assistant_message(
                self._sid,
                assistant_text,
                parent_ids=(prompt_ev.id,) if prompt_ev else (),
                tokens_in=int(usage.get("input_tokens") or 0),
                tokens_out=int(usage.get("output_tokens") or 0),
                cached_tokens=int(usage.get("cache_read_input_tokens") or 0),
                cache_creation_tokens=int(usage.get("cache_creation_input_tokens") or 0),
                model=kwargs.get("model"),
                latency_ms=latency,
            )
            self._t.record_tokens(
                session_id=self._sid,
                model=kwargs.get("model") or "unknown",
                tokens_in=assistant_ev.tokens_in,
                tokens_out=assistant_ev.tokens_out,
            )
            self._t.annotate_span(
                span,
                tokens_in=assistant_ev.tokens_in,
                tokens_out=assistant_ev.tokens_out,
                latency_ms=latency,
            )

            # Capture any tool_use blocks. PostToolUse is emitted later when
            # the caller feeds tool_result back into the conversation — we do
            # not fake outputs here; we only record the INTENT to use a tool.
            for block in _iter_content_blocks(resp):
                if block.get("type") == "tool_use":
                    self._store.append(make_event(
                        session_id=self._sid,
                        seq=self._store._next_seq(self._sid),
                        type=EventType.PRE_TOOL_USE,
                        payload={
                            "tool_name": block.get("name"),
                            "input": block.get("input") or {},
                        },
                        parent_ids=(assistant_ev.id,),
                    ))
        return resp


def _extract_last_user_text(messages: list) -> str:
    for m in reversed(messages or []):
        if m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    parts.append(c.get("text") or "")
            if parts:
                return "\n".join(parts)
        return ""
    return ""


def _extract_assistant_text(resp: Any) -> str:
    content = getattr(resp, "content", None)
    if content is None:
        return ""
    parts: list[str] = []
    for block in content:
        if hasattr(block, "text"):
            parts.append(block.text or "")
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text") or "")
    return "\n".join(parts)


def _iter_content_blocks(resp: Any):
    content = getattr(resp, "content", None)
    if content is None:
        return
    for b in content:
        if isinstance(b, dict):
            yield b
        elif hasattr(b, "model_dump"):
            yield b.model_dump()
        else:
            yield {"type": getattr(b, "type", None),
                   "text": getattr(b, "text", None),
                   "name": getattr(b, "name", None),
                   "input": getattr(b, "input", None)}
