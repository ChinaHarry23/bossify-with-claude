#!/usr/bin/env python3
"""Stop hook — captures final assistant message + token usage."""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
from _shim import bootstrap, safe_run  # noqa: E402


def _run():
    args, payload = bootstrap()
    from token_roi.hooks import on_stop
    from token_roi.storage import EventStore
    store = EventStore(args.data_dir)
    on_stop(payload, store)


if __name__ == "__main__":
    sys.exit(safe_run(_run))
