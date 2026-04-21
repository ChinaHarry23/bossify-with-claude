"""Import Cursor per-turn usage from the CSV exported by cursor.com.

Cursor subscription users don't get per-turn token counts in their local
``state.vscdb`` — those only appear in BYOK mode. But Cursor's website
(cursor.com/dashboard/usage) lets you export a CSV of every billable
turn with exact token counts and the model used. This importer reads
that CSV so Bossify can price Cursor activity alongside Claude Code and
Codex.

Shape of the CSV (header row):

    Date, Cloud Agent ID, Automation ID, Kind, Model, Max Mode,
    Input (w/ Cache Write), Input (w/o Cache Write),
    Cache Read, Output Tokens, Total Tokens, Cost

Each row is one turn. We group rows by calendar date into a synthetic
session (``cursor-YYYY-MM-DD``) so the dashboard shows roughly one card
per day of Cursor use instead of one per turn. The session is tagged
``platform="cursor"`` — the same bucket as the state.vscdb importer —
so "Cursor" on the dashboard aggregates across both sources.

Only cost data is imported from the CSV; there is no prompt / response
text here (Cursor's export doesn't include message bodies). If you also
want prompt content, run ``token-roi import cursor`` in addition.
"""
from __future__ import annotations

import csv
import datetime as _dt
import logging
from pathlib import Path

from ..events import EventType, make_event
from . import ImportStats, Importer, register

log = logging.getLogger(__name__)


@register
class CursorUsageImporter(Importer):
    source_name = "cursor-usage"

    @classmethod
    def default_path(cls) -> Path:
        # Cursor's download button writes to ~/Downloads with a
        # date-stamped name like usage-events-2026-04-20.csv. Pick the
        # most recent one so `token-roi import cursor-usage` works with
        # no flags after a fresh download.
        downloads = Path("~/Downloads").expanduser()
        if not downloads.exists():
            return downloads
        matches = sorted(downloads.glob("usage-events-*.csv"))
        return matches[-1] if matches else downloads / "usage-events-*.csv"

    def import_path(
        self, path: Path | str, *, project_filter: str | None = None
    ) -> ImportStats:
        p = Path(path).expanduser()
        stats = ImportStats()
        if p.is_dir():
            # A directory was handed in — find the newest CSV in it.
            matches = sorted(p.glob("usage-events-*.csv"))
            if not matches:
                raise FileNotFoundError(
                    f"no Cursor usage CSV found in {p} "
                    f"(expected usage-events-*.csv)"
                )
            p = matches[-1]
        if not p.is_file():
            raise FileNotFoundError(f"no Cursor usage CSV at {p}")

        self._import_csv(p, stats)
        return stats

    # ---- internals ----

    def _import_csv(self, csv_path: Path, stats: ImportStats) -> None:
        stats.files += 1
        # Bucket rows by calendar date so each Cursor day becomes a
        # session. Deterministic session ids → re-running the import
        # produces the same event ids, so repeat imports are safe.
        rows_by_day: dict[str, list[dict]] = {}
        with csv_path.open("r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                stats.lines += 1
                ts_raw = (row.get("Date") or "").strip()
                if not ts_raw:
                    stats.skipped += 1
                    continue
                try:
                    dt = _dt.datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                except Exception:
                    stats.skipped += 1
                    continue
                day = dt.date().isoformat()
                rows_by_day.setdefault(day, []).append({"_dt": dt, **row})

        # Grouping is for display (one session per day of Cursor use);
        # project_slug is a fixed "cursor-web" bucket since the CSV
        # doesn't carry workspace/project info. The manager dashboard
        # will show one "Cursor Web Usage" project card with real cost.
        project_slug = "cursor-web"
        employee_id = None
        if self.employees is not None:
            employee_id = self.employees.resolve_for_slug(project_slug).id

        for day, day_rows in sorted(rows_by_day.items()):
            session_id = f"cursor-{day}"
            self.store.start_session(session_id)
            if self.db is not None:
                # platform="cursor" — NOT the importer's source_name
                # ("cursor-usage"), so the dashboard aggregates this
                # under the same "Cursor" platform as state.vscdb imports.
                self.db.upsert_session_metadata(
                    session_id,
                    project_slug=project_slug,
                    employee_id=employee_id,
                    platform="cursor",
                )

            for row in sorted(day_rows, key=lambda r: r["_dt"]):
                ts = row["_dt"].timestamp()
                kind = (row.get("Kind") or "").strip()
                # Drop rows that were billed $0 because of an error —
                # they're not real work. "Included"/"Free"/"Overage"
                # are all kept because they represent real turns.
                if kind.lower().startswith("errored"):
                    stats.skipped += 1
                    continue

                model = (row.get("Model") or "").strip() or None
                # "Input (w/o Cache Write)" is the fresh (non-cached)
                # input tokens. "Input (w/ Cache Write)" minus that
                # equals the cache_creation tokens, which Anthropic
                # prices at 1.25x input. Cursor labels them together
                # so we back them out.
                input_fresh = _int(row.get("Input (w/o Cache Write)"))
                input_all   = _int(row.get("Input (w/ Cache Write)"))
                cache_create = max(0, input_all - input_fresh)
                cache_read   = _int(row.get("Cache Read"))
                output       = _int(row.get("Output Tokens"))

                # The CSV has no prompt/response text. We emit one
                # ASSISTANT_MESSAGE per row carrying the real token
                # counts + model; that's enough for cost rollups and
                # per-platform breakdowns. No USER_PROMPT event is
                # emitted (judge / attribution don't apply — there's
                # no prompt to judge).
                self.store.append(make_event(
                    session_id=session_id,
                    seq=self.store.next_seq(session_id),
                    type=EventType.ASSISTANT_MESSAGE,
                    payload={
                        "text": f"[Cursor turn @ {row['_dt'].isoformat()} · {kind}]",
                        "source": "cursor-usage-csv",
                        "max_mode": (row.get("Max Mode") or "").strip() or None,
                    },
                    tokens_in=input_fresh,
                    tokens_out=output,
                    cached_tokens=cache_read,
                    cache_creation_tokens=cache_create,
                    model=model,
                    ts=ts,
                ))
                stats.assistant_messages += 1
                stats.events_written += 1


def _int(v: object) -> int:
    if v is None or v == "":
        return 0
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0
