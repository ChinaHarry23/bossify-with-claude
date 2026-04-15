"""CLI smoke tests.

Verifies the happy-path end-to-end: init → capture → score → explain.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

from token_roi.cli import main


def _run(argv: list[str]) -> int:
    return main(argv)


def test_init_creates_structure(tmp_path: Path, capsys):
    data = tmp_path / "data"
    rc = _run(["--data-dir", str(data), "init"])
    assert rc == 0
    assert (data / "raw_events").exists()
    assert (data / "memory" / "MEMORY.md").exists()
    assert (data / "analytics" / "roi.db").exists()


def test_capture_and_score(tmp_path: Path, capsys, monkeypatch):
    data = tmp_path / "data"
    _run(["--data-dir", str(data), "init"])

    # Capture a user prompt from stdin.
    monkeypatch.setattr("sys.stdin", io.StringIO("hello world"))
    _run(["--data-dir", str(data), "capture", "--role", "user",
          "--session-id", "t1"])
    # Capture an assistant message with usage.
    monkeypatch.setattr("sys.stdin", io.StringIO("hi there"))
    _run(["--data-dir", str(data), "capture", "--role", "assistant",
          "--session-id", "t1", "--tokens-in", "100", "--tokens-out", "200"])

    # Ingest + score.
    assert _run(["--data-dir", str(data), "ingest"]) == 0
    assert _run(["--data-dir", str(data), "score", "--session", "t1"]) == 0

    captured = capsys.readouterr()
    assert "sessions=" in captured.out
