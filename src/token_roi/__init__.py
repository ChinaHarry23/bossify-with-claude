"""Bossify with Claude (新时代老板) — local-first flight recorder + ROI analyzer for agentic sessions.

The `token_roi` package is the underlying engine that powers the
``bossify-with-claude`` Claude Code skill. Public surface is intentionally
narrow: most callers should use the ``token-roi`` CLI. The library is
re-exported here for in-process use (Agent SDK wrapper, tests).
"""
from __future__ import annotations

__version__ = "0.1.0"

from .events import Event, EventType, make_event
from .storage import EventStore
from .db import AnalyticsDB
from .memory import MemoryLayer
from .retrieval import RetrievalIndex, RetrievalResult
from .attribution import AttributionGraph, Attribution
from .roi import ROIClassifier, ROIClass, ROIScore
from .compression import CompressionEngine
from .replay import Replayer

__all__ = [
    "Event",
    "EventType",
    "make_event",
    "EventStore",
    "AnalyticsDB",
    "MemoryLayer",
    "RetrievalIndex",
    "RetrievalResult",
    "AttributionGraph",
    "Attribution",
    "ROIClassifier",
    "ROIClass",
    "ROIScore",
    "CompressionEngine",
    "Replayer",
]
