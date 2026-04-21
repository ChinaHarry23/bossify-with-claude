"""Import Cursor IDE chat history.

Cursor stores chat history in SQLite databases under platform-specific
``User/workspaceStorage/<hash>/state.vscdb`` files (and a ``globalStorage``
counterpart). Chat messages live in the ``ItemTable`` table with keys like
``workbench.panel.aichat.view.aichat.chatdata`` or ``composer.composerData``.
The value is JSON, with tabs/conversations containing message lists.

Best effort, schema may vary across Cursor versions. The importer is
defensive — malformed rows are skipped rather than raising.

Cursor sessions typically lack token usage. We emit USER_PROMPT and
ASSISTANT_MESSAGE events with zero tokens unless the chat data carries a
``usage`` block (which only happens in API-key/OpenRouter mode).
"""
from __future__ import annotations

import json
import logging
import sys
import sqlite3
from pathlib import Path

from ..events import EventType, is_synthetic_prompt, make_event
from . import ImportStats, Importer, register

log = logging.getLogger(__name__)


@register
class CursorImporter(Importer):
    source_name = "cursor"

    @classmethod
    def default_path(cls) -> Path:
        if sys.platform == "darwin":
            return Path("~/Library/Application Support/Cursor/User").expanduser()
        if sys.platform.startswith("win"):
            import os
            base = os.environ.get("APPDATA", "")
            return Path(base) / "Cursor" / "User"
        return Path("~/.config/Cursor/User").expanduser()

    def import_path(
        self, path: Path | str, *, project_filter: str | None = None
    ) -> ImportStats:
        p = Path(path).expanduser()
        stats = ImportStats()
        dbs: list[Path] = []
        if p.is_file() and p.suffix == ".vscdb":
            dbs = [p]
        elif p.is_dir():
            dbs = sorted(p.rglob("state.vscdb"))
        else:
            raise FileNotFoundError(f"no Cursor state.vscdb found at {p}")
        for db in dbs:
            if project_filter and project_filter not in str(db):
                continue
            try:
                self._import_db(db, stats)
            except Exception as e:  # defensive
                log.warning("skip cursor db %s: %s", db, e)
                stats.skipped += 1
        return stats

    # ---- internals ----

    def _import_db(self, db_path: Path, stats: ImportStats) -> None:
        stats.files += 1
        workspace_hash = db_path.parent.name
        # Try to recover original project path from workspace.json sibling.
        project_slug = workspace_hash
        wsjson = db_path.parent / "workspace.json"
        if wsjson.exists():
            try:
                meta = json.loads(wsjson.read_text("utf-8"))
                folder = meta.get("folder") or meta.get("configuration") or ""
                if folder:
                    project_slug = Path(str(folder).rstrip("/")).name or workspace_hash
            except Exception:
                pass

        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        warned = False
        try:
            cur = conn.cursor()
            try:
                cur.execute(
                    "SELECT key, value FROM ItemTable "
                    "WHERE key LIKE '%aichat%' OR key LIKE '%composer%'"
                )
                rows = cur.fetchall()
            except sqlite3.Error as e:
                log.warning("ItemTable query failed on %s: %s", db_path, e)
                stats.skipped += 1
                return

            conv_idx = 0
            for key, value in rows:
                stats.lines += 1
                try:
                    blob = json.loads(value)
                except Exception:
                    stats.skipped += 1
                    continue
                # blob shape varies — look for a list of conversations/tabs.
                conversations = _find_conversations(blob)
                if not conversations:
                    stats.skipped += 1
                    continue
                for conv in conversations:
                    cid = (
                        conv.get("id")
                        or conv.get("tabId")
                        or conv.get("composerId")
                        or f"cursor-{workspace_hash}-{conv_idx}"
                    )
                    conv_idx += 1
                    session_id = str(cid)
                    self.store.start_session(session_id)
                    employee_id = None
                    if self.employees is not None:
                        employee_id = self.employees.resolve_for_slug(project_slug).id
                    if self.db is not None:
                        self.db.upsert_session_metadata(
                            session_id,
                            project_slug=project_slug,
                            employee_id=employee_id,
                            platform=self.source_name,
                        )
                    messages = (
                        conv.get("messages")
                        or conv.get("conversation")
                        or conv.get("bubbles")
                        or []
                    )
                    last_pre_id = ""
                    for msg in messages:
                        if not isinstance(msg, dict):
                            stats.skipped += 1
                            continue
                        role = (
                            msg.get("role")
                            or ("user" if msg.get("type") == 1 else
                                "assistant" if msg.get("type") == 2 else None)
                        )
                        text = msg.get("text") or msg.get("content") or ""
                        if isinstance(text, list):
                            text = "\n".join(
                                b.get("text", "") if isinstance(b, dict) else str(b)
                                for b in text
                            )
                        ts = float(msg.get("timestamp") or 0) / (
                            1000.0 if isinstance(msg.get("timestamp"), int)
                            and msg.get("timestamp") > 1e12 else 1.0
                        ) if msg.get("timestamp") else 0.0
                        if role == "user":
                            if is_synthetic_prompt(text):
                                stats.synthetic_prompts_dropped += 1
                                continue
                            self.store.append(make_event(
                                session_id=session_id,
                                seq=self.store.next_seq(session_id),
                                type=EventType.USER_PROMPT,
                                payload={"text": text},
                                ts=ts,
                            ))
                            stats.user_prompts += 1
                            stats.events_written += 1
                        elif role == "assistant":
                            usage = msg.get("usage") or {}
                            if not usage and not warned:
                                log.warning(
                                    "Cursor session %s has no token usage; "
                                    "enable API-key mode in Cursor for cost data.",
                                    db_path,
                                )
                                warned = True
                            self.store.append(make_event(
                                session_id=session_id,
                                seq=self.store.next_seq(session_id),
                                type=EventType.ASSISTANT_MESSAGE,
                                payload={"text": text},
                                tokens_in=int(usage.get("input_tokens") or 0),
                                tokens_out=int(usage.get("output_tokens") or 0),
                                cached_tokens=int(usage.get("cached_input_tokens") or 0),
                                model=msg.get("model"),
                                ts=ts,
                            ))
                            stats.assistant_messages += 1
                            stats.events_written += 1
                            # inline tool/function blocks, if any
                            for fn in _iter_function_blocks(msg):
                                pre = self.store.append(make_event(
                                    session_id=session_id,
                                    seq=self.store.next_seq(session_id),
                                    type=EventType.PRE_TOOL_USE,
                                    payload={
                                        "tool_name": fn.get("name") or "unknown",
                                        "input": fn.get("arguments") or {},
                                    },
                                    ts=ts,
                                ))
                                last_pre_id = pre.id
                                stats.tool_uses += 1
                                stats.events_written += 1
                                if "output" in fn:
                                    self.store.append(make_event(
                                        session_id=session_id,
                                        seq=self.store.next_seq(session_id),
                                        type=EventType.POST_TOOL_USE,
                                        payload={
                                            "tool_name": fn.get("name") or "unknown",
                                            "output": fn.get("output"),
                                            "success": True,
                                        },
                                        parent_ids=(last_pre_id,),
                                        ts=ts,
                                    ))
                                    stats.tool_results += 1
                                    stats.events_written += 1
                        else:
                            stats.skipped += 1
        finally:
            conn.close()


def _find_conversations(blob: object) -> list[dict]:
    """Heuristically locate a list of conversation dicts within Cursor's blob."""
    if isinstance(blob, dict):
        for key in ("tabs", "conversations", "composers", "allComposers"):
            v = blob.get(key)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
        # single conversation at top level
        if any(k in blob for k in ("messages", "conversation", "bubbles")):
            return [blob]
    if isinstance(blob, list) and blob and isinstance(blob[0], dict):
        return blob
    return []


def _iter_function_blocks(msg: dict) -> list[dict]:
    out: list[dict] = []
    for k in ("toolCalls", "tool_calls", "functionCalls", "functions"):
        v = msg.get(k)
        if isinstance(v, list):
            out.extend(x for x in v if isinstance(x, dict))
    content = msg.get("content")
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") in {"function", "tool_call"}:
                out.append(b)
    return out
