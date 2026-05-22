"""Shared pytest fixtures.

Every test gets a fresh, isolated data dir under pytest's tmp_path. The
fixture wires up `EventStore`, `AnalyticsDB`, and `MemoryLayer` so tests
can read/write through the real code paths without hand-wiring plumbing.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


# Ensure the package is importable from src/ when running tests without install.
_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    (d / "raw_events").mkdir(parents=True)
    (d / "snapshots").mkdir(parents=True)
    (d / "memory" / "topics").mkdir(parents=True)
    (d / "retrieval" / "embeddings").mkdir(parents=True)
    (d / "retrieval" / "indexes").mkdir(parents=True)
    (d / "analytics").mkdir(parents=True)
    return d


@pytest.fixture()
def store(data_dir):
    from token_roi.storage import EventStore
    return EventStore(data_dir)


@pytest.fixture()
def db(data_dir):
    from token_roi.db import AnalyticsDB
    db = AnalyticsDB(data_dir / "analytics" / "roi.db")
    db.migrate()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def memory(data_dir, store):
    from token_roi.memory import MemoryLayer
    return MemoryLayer(data_dir / "memory", store=store)
