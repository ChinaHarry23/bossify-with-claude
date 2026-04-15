#!/usr/bin/env python3
"""PostToolUse hook — captures tool result + derives file/memory events."""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
from _shim import bootstrap, safe_run  # noqa: E402


def _run():
    args, payload = bootstrap()
    from token_roi.hooks import on_post_tool_use
    from token_roi.memory import MemoryLayer
    from token_roi.storage import EventStore
    store = EventStore(args.data_dir)
    memory_dir = Path(args.memory_dir) if args.memory_dir else Path(args.data_dir) / "memory"
    memory = MemoryLayer(memory_dir, store=store)
    on_post_tool_use(payload, store, memory)


if __name__ == "__main__":
    sys.exit(safe_run(_run))
