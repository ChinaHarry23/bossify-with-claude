"""Importer plugin registry.

Each external source (Claude Code, Codex, Cursor, Aider, OpenAI JSONL) is
implemented as a subclass of :class:`Importer` that self-registers via the
``@register`` decorator when its module is imported.

Shared :class:`ImportStats` lives here so every importer reports the same
shape of counters to the CLI.
"""
from __future__ import annotations

import abc
import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Type

from ..db import AnalyticsDB
from ..employees import EmployeeRegistry
from ..storage import EventStore


@dataclass
class ImportStats:
    files: int = 0
    lines: int = 0
    events_written: int = 0
    user_prompts: int = 0
    assistant_messages: int = 0
    tool_uses: int = 0
    tool_results: int = 0
    file_reads: int = 0
    file_writes: int = 0
    skipped: int = 0
    synthetic_prompts_dropped: int = 0

    def to_dict(self) -> dict:
        return self.__dict__.copy()


class Importer(abc.ABC):
    """Abstract base class for all session-log importers."""

    source_name: str = ""

    def __init__(
        self,
        store: EventStore,
        *,
        db: AnalyticsDB | None = None,
        employees: EmployeeRegistry | None = None,
    ) -> None:
        self.store = store
        self.db = db
        self.employees = employees

    @classmethod
    def default_path(cls) -> Path:
        """Default filesystem location to scan for this source."""
        raise NotImplementedError

    @abc.abstractmethod
    def import_path(
        self, path: Path | str, *, project_filter: str | None = None
    ) -> ImportStats:
        ...


# --- registry ---------------------------------------------------------------

_REGISTRY: dict[str, Type[Importer]] = {}


def register(cls: Type[Importer]) -> Type[Importer]:
    """Class decorator that records ``cls`` in the importer registry."""
    name = getattr(cls, "source_name", None)
    if not name:
        raise ValueError(f"{cls.__name__} must set source_name")
    _REGISTRY[name] = cls
    return cls


def _ensure_loaded() -> None:
    # Import each importer module so its @register side-effect runs.
    for mod in (
        "claude_code",
        "codex",
        "cursor",
        "aider",
        "openai_jsonl",
    ):
        try:
            importlib.import_module(f"{__name__}.{mod}")
        except Exception:  # pragma: no cover — keep CLI usable even if one fails
            pass


def list_sources() -> list[str]:
    _ensure_loaded()
    return sorted(_REGISTRY.keys())


def get_importer(
    source_name: str,
    store: EventStore,
    *,
    db: AnalyticsDB | None = None,
    employees: EmployeeRegistry | None = None,
) -> Importer:
    _ensure_loaded()
    try:
        cls = _REGISTRY[source_name]
    except KeyError as e:
        raise ValueError(f"unknown importer source: {source_name}") from e
    return cls(store, db=db, employees=employees)
