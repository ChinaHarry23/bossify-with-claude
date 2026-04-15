"""Employee registry for the manager-oriented dashboard.

The "employee" concept is an **overlay** on top of session_summaries — it is
purely for aggregation in the UI. Underlying events, attributions, and ROI
scores are completely unaware of it. This keeps the ROI math identical to
the single-user case and means a future change to the employee model
(add/remove employees, rename, re-assign projects) is just a config edit +
a re-ingest.

Resolution order, highest priority first:
    1. `data/employees.json` has a `project_to_employee` mapping for the
       project slug   → use that employee's entry.
    2. `data/employees.json` has a `default_employee` field              → use it.
    3. No config file                                                     → synthesize one
       employee whose id is the current OS user and whose display name
       equals the id (so the dashboard shows "chinaharry" instead of a
       random hash).

The minimal config file looks like this:

    {
      "employees": {
        "chinaharry": {
          "name": "陈俊",
          "role": "全栈工程师",
          "team": "平台组"
        }
      },
      "project_to_employee": {
        "-Users-chinaharry-Desktop-Workspace-Claude-code-pg-Bossify": "chinaharry"
      },
      "default_employee": "chinaharry"
    }

Managers can grow the file as the team grows — no code changes needed.
"""
from __future__ import annotations

import getpass
import json
import logging
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Employee:
    id: str
    name: str
    role: str | None = None
    team: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class EmployeeRegistry:
    """In-memory wrapper around `data/employees.json`.

    Construct once at CLI / dashboard boot. Reads the config file if
    present; otherwise synthesizes a single-employee registry around the
    OS user so the demo "just works" on a fresh machine.
    """

    CONFIG_FILE = "employees.json"

    def __init__(self, data_dir: Path | str):
        self.data_dir = Path(data_dir)
        self.config_path = self.data_dir / self.CONFIG_FILE
        self._employees: dict[str, Employee] = {}
        self._project_to_id: dict[str, str] = {}
        self._default_id: str = ""
        self._load()

    # ---- loading ----

    def _load(self) -> None:
        if self.config_path.exists():
            try:
                raw = json.loads(self.config_path.read_text(encoding="utf-8"))
                self._ingest_config(raw)
                return
            except Exception as e:  # noqa: BLE001
                log.warning("employees.json unreadable (%s) — falling back to OS user", e)

        # No config → synthesize the default single-employee case.
        uid = getpass.getuser() or "unknown"
        me = Employee(id=uid, name=uid)
        self._employees = {uid: me}
        self._default_id = uid

    def _ingest_config(self, raw: dict) -> None:
        self._employees = {}
        for emp_id, data in (raw.get("employees") or {}).items():
            self._employees[emp_id] = Employee(
                id=emp_id,
                name=(data.get("name") or emp_id),
                role=data.get("role"),
                team=data.get("team"),
            )
        self._project_to_id = dict(raw.get("project_to_employee") or {})
        self._default_id = (
            raw.get("default_employee")
            or (next(iter(self._employees)) if self._employees else getpass.getuser())
        )
        # Ensure the default employee actually exists (helps catch typos).
        if self._default_id not in self._employees:
            uid = self._default_id
            self._employees.setdefault(uid, Employee(id=uid, name=uid))

    # ---- public API ----

    def all(self) -> list[Employee]:
        return sorted(self._employees.values(), key=lambda e: e.name.lower())

    def get(self, employee_id: str) -> Employee | None:
        return self._employees.get(employee_id)

    def default(self) -> Employee:
        return self._employees[self._default_id]

    def resolve_for_slug(self, project_slug: str | None) -> Employee:
        """Map a Claude Code project slug → Employee. Unknown slug falls back
        to the default employee."""
        if project_slug and project_slug in self._project_to_id:
            mapped = self._project_to_id[project_slug]
            if mapped in self._employees:
                return self._employees[mapped]
        return self.default()

    # ---- persistence (used by `token-roi employees` CLI down the road) ----

    def to_config_dict(self) -> dict:
        return {
            "employees": {e.id: {k: v for k, v in e.to_dict().items() if k != "id" and v}
                           for e in self._employees.values()},
            "project_to_employee": dict(self._project_to_id),
            "default_employee": self._default_id,
        }

    def save(self) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps(self.to_config_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return self.config_path


def format_employee_table(employees: Iterable[Employee]) -> str:
    """Small helper for the `token-roi employees list` CLI output."""
    rows = [(e.id, e.name, e.role or "—", e.team or "—") for e in employees]
    if not rows:
        return "(no employees)"
    widths = [max(len(r[i]) for r in rows + [("ID", "NAME", "ROLE", "TEAM")]) for i in range(4)]
    header = f"{'ID'.ljust(widths[0])}  {'NAME'.ljust(widths[1])}  {'ROLE'.ljust(widths[2])}  {'TEAM'.ljust(widths[3])}"
    sep = "-" * len(header)
    body = "\n".join(
        f"{r[0].ljust(widths[0])}  {r[1].ljust(widths[1])}  {r[2].ljust(widths[2])}  {r[3].ljust(widths[3])}"
        for r in rows
    )
    return f"{header}\n{sep}\n{body}"
