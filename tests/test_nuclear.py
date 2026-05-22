"""End-to-end tests for the ``token-roi nuclear`` command.

We can't exercise the LLM-dependent steps in CI (no local model), so
every test uses ``--skip-judge --skip-name`` to smoke the destructive +
init + import + score path. That still exercises the dangerous bits:
the wipe, the employees.json backup/restore, and the subprocess
pipeline fail-fast behaviour.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _run_nuclear(data_dir: Path, *extra_args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable, "-m", "token_roi.cli",
            "--data-dir", str(data_dir),
            "nuclear", "--yes", "--skip-judge", "--skip-name",
            *extra_args,
        ],
        capture_output=True,
        text=True,
    )


def test_nuclear_wipes_and_reinits(tmp_path):
    """Junk files under the data dir disappear; a valid empty data dir
    comes out the other side."""
    d = tmp_path / "data"
    d.mkdir()
    # Seed some junk so we can prove it was wiped.
    (d / "junk.txt").write_text("should not survive")
    (d / "analytics").mkdir()
    (d / "analytics" / "stale.db").write_text("stale")

    r = _run_nuclear(d)
    assert r.returncode == 0, r.stderr

    # Data dir was re-created by `init`.
    assert d.exists()
    assert not (d / "junk.txt").exists()
    assert not (d / "analytics" / "stale.db").exists()


def test_nuclear_preserves_employees_json(tmp_path):
    """employees.json is backed up before the wipe and restored after
    init. The importer-tagging step relies on this — otherwise the
    first `import` after `nuclear` would drop all employee mappings."""
    d = tmp_path / "data"
    d.mkdir()
    config = {
        "employees": {
            "alice": {"name": "Alice Wang", "role": "Eng", "team": "X"},
        },
        "default_employee": "alice",
    }
    (d / "employees.json").write_text(json.dumps(config))

    r = _run_nuclear(d)
    assert r.returncode == 0, r.stderr

    # The file is back with its original contents.
    assert (d / "employees.json").exists()
    assert json.loads((d / "employees.json").read_text()) == config


def test_nuclear_without_employees_json_is_fine(tmp_path):
    """No employees.json to back up → nuclear still works."""
    d = tmp_path / "data"
    d.mkdir()
    (d / "scratch").mkdir()

    r = _run_nuclear(d)
    assert r.returncode == 0, r.stderr
    # No employees.json was created out of nowhere.
    assert not (d / "employees.json").exists()


def test_nuclear_without_yes_flag_aborts_on_empty_stdin(tmp_path):
    """When stdin is closed and --yes is missing, nuclear must refuse
    to run — the safety gate is the whole point of the command."""
    d = tmp_path / "data"
    d.mkdir()
    marker = d / "I_AM_HERE"
    marker.write_text("ok")

    r = subprocess.run(
        [
            sys.executable, "-m", "token_roi.cli",
            "--data-dir", str(d),
            "nuclear", "--skip-judge", "--skip-name",
        ],
        input="",                    # closed stdin → EOFError in input()
        capture_output=True,
        text=True,
    )
    assert r.returncode != 0
    # Most importantly, the data dir wasn't touched.
    assert marker.exists()
