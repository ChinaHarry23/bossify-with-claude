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


def _maybe_data_dir_from_argv() -> Path | None:
    """Re-parse --data-dir from sys.argv so safe_run can find it without
    the caller threading it through. We swallow any parse error because
    this is only for the breadcrumb file."""
    try:
        p = argparse.ArgumentParser(add_help=False)
        p.add_argument("--data-dir")
        parsed, _ = p.parse_known_args()
        return Path(parsed.data_dir) if parsed.data_dir else None
    except Exception:
        return None


def safe_run(fn, *args, **kwargs) -> int:
    """Run a hook function, swallow any exception, log, return exit code.

    Returns 0 on success (or suppressed failure). Claude Code cares only
    about nonzero exit + stderr — we never want to raise. We also append
    a breadcrumb to ``<data-dir>/hook_failures.log`` so silent hook
    breakage is visible later without scraping Claude Code's stderr.
    """
    try:
        fn(*args, **kwargs)
        return 0
    except Exception as e:  # noqa: BLE001
        logging.getLogger("token_roi.hook").exception("hook failed: %s", e)
        try:
            data_dir = _maybe_data_dir_from_argv()
            if data_dir is not None:
                import time
                import traceback
                log_path = data_dir / "hook_failures.log"
                log_path.parent.mkdir(parents=True, exist_ok=True)
                fn_name = getattr(fn, "__qualname__", getattr(fn, "__name__", "hook"))
                with log_path.open("a", encoding="utf-8") as lf:
                    lf.write(f"{time.time():.0f}\t{fn_name}\t{type(e).__name__}: {e}\n")
                    lf.write(traceback.format_exc())
                    lf.write("---\n")
        except Exception:
            # Breadcrumb write must never mask the original failure —
            # the stderr log above is still the primary signal.
            pass
        # Return 0 anyway — a broken hook should not break the user's session.
        return 0
