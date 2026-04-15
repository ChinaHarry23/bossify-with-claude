"""Shared bootstrap for every hook script.

Each hook script is invoked by Claude Code with stdin=JSON payload and a
`--data-dir` flag. The hook resolves the token_roi package on sys.path, then
dispatches into the typed handler in `token_roi.hooks`.

Hooks MUST NOT raise on failure — a broken hook would break the user's
Claude Code session. We swallow + log to stderr, which Claude Code surfaces
as a warning rather than a hard stop.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path


def bootstrap() -> tuple[argparse.Namespace, dict]:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--memory-dir", default=None,
                        help="defaults to <data-dir>/memory")
    parser.add_argument("--debug", action="store_true")
    args, _ = parser.parse_known_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="[token_roi.hook %(name)s] %(message)s",
        stream=sys.stderr,
    )

    # Add src/ to path if running without pip install.
    here = Path(__file__).resolve().parent
    src = here.parent / "src"
    if src.exists() and str(src) not in sys.path:
        sys.path.insert(0, str(src))

    from token_roi.hooks import load_payload
    payload = load_payload()
    return args, payload


def safe_run(fn, *args, **kwargs) -> int:
    """Run a hook function, swallow any exception, log, return exit code.

    Returns 0 on success (or suppressed failure). Claude Code cares only
    about nonzero exit + stderr — we never want to raise.
    """
    try:
        fn(*args, **kwargs)
        return 0
    except Exception as e:  # noqa: BLE001
        logging.getLogger("token_roi.hook").exception("hook failed: %s", e)
        # Return 0 anyway — a broken hook should not break the user's session.
        return 0
