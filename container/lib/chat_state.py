"""
Which wolt and session each chat talks to, in lodge.db's chat_state table.

A chat is (adapter, chat_id). `active_wolt` and `active_session` are columns;
any other key the adapter keeps rides along in `record`.
"""

from __future__ import annotations

import json
import sys
import time
from contextlib import closing
from pathlib import Path

import lodge_db


def get(wolts_dir, adapter: str, chat_id) -> dict:
    """The chat's state, or {} for a chat never saved."""
    with closing(lodge_db.connect(wolts_dir)) as conn:
        row = conn.execute(
            "SELECT active_wolt, active_session, record FROM chat_state"
            " WHERE adapter=? AND chat_id=?", (adapter, str(chat_id)),
        ).fetchone()
    if row is None:
        return {}
    state = json.loads(row["record"] or "{}")
    for key in ("active_wolt", "active_session"):
        if row[key]:
            state[key] = row[key]
    return state


_INSERT = ("INTO chat_state (adapter, chat_id, active_wolt, active_session, record, updated_at)"
           " VALUES (?, ?, ?, ?, ?, ?)")


def _row(adapter: str, chat_id, state: dict) -> tuple:
    extra = {k: v for k, v in state.items() if k not in ("active_wolt", "active_session")}
    return (adapter, str(chat_id), state.get("active_wolt"), state.get("active_session"),
            json.dumps(extra), int(time.time()))


def put(wolts_dir, adapter: str, chat_id, state: dict) -> None:
    """Replace the chat's state with `state`."""
    with lodge_db.write(wolts_dir) as conn:
        conn.execute("INSERT OR REPLACE " + _INSERT, _row(adapter, chat_id, state))


def delete(wolts_dir, adapter: str, chat_id) -> None:
    with lodge_db.write(wolts_dir) as conn:
        conn.execute("DELETE FROM chat_state WHERE adapter=? AND chat_id=?",
                     (adapter, str(chat_id)))


def forget_session(wolts_dir, adapter: str, session: str) -> int:
    """Clear `session` from every chat of `adapter` that talks to it; how many."""
    with lodge_db.write(wolts_dir) as conn:
        return conn.execute(
            "UPDATE chat_state SET active_session=NULL, updated_at=?"
            " WHERE adapter=? AND active_session=?",
            (int(time.time()), adapter, session),
        ).rowcount


_PENDING_COLUMNS = ("user", "channel", "root_ts", "picker_ts", "event_id", "text", "state",
                    "wolt", "session", "created_at", "expires_at")


def import_chat(conn, wolts_dir) -> tuple[dict, list[Path]]:
    """Import the chat files older releases wrote; lodge_db.run_imports retires them.

    Telegram: .space/telegram/chats/<chat_id>.json. Slack, under whichever wolt
    dir the adapter ran as: _thread_sessions.json, _owner_selections.json,
    _pending_messages.json (and _active_threads.json, which nothing read).
    A row already in the table wins over a file.
    """
    counts = {"imported": 0, "skipped": 0}
    retire: list[Path] = []

    def load(path: Path) -> dict:
        """The file's JSON object, retired once read; {} (and kept) if unreadable."""
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
        except (OSError, ValueError) as exc:
            print(f"[lodge_db] chat import: skipping unreadable {path}: {exc}", file=sys.stderr)
            counts["skipped"] += 1
            return {}
        retire.append(path)
        return data

    def entries(path: Path, kind: type):
        """The file's entries whose value is a `kind`; any other is counted skipped."""
        if not path.is_file():
            return
        for key, value in load(path).items():
            if isinstance(value, kind):
                yield key, value
            else:
                counts["skipped"] += 1

    def insert(sql: str, row: tuple) -> None:
        counts["imported" if conn.execute(sql, row).rowcount else "skipped"] += 1

    for path in sorted((Path(wolts_dir) / ".space" / "telegram" / "chats").glob("*.json")):
        state = {k: v for k, v in load(path).items() if k not in ("chat_id", "updated_at")}
        if state:
            insert("INSERT OR IGNORE " + _INSERT, _row("telegram", path.stem, state))

    for slack in sorted(Path(wolts_dir).glob("*/.state/chat/slack")):
        for key, owner in entries(slack / "_thread_sessions.json", dict):
            state = {"active_wolt": owner.pop("wolt", None),
                     "active_session": owner.pop("session", None), **owner}
            insert("INSERT OR IGNORE " + _INSERT, _row("slack", key, state))
        for user, wolt in entries(slack / "_owner_selections.json", str):
            insert("INSERT OR IGNORE " + _INSERT, _row("slack", user, {"active_wolt": wolt}))
        for key, record in entries(slack / "_pending_messages.json", dict):
            insert("INSERT OR IGNORE INTO pending_messages (id, adapter, "
                   + ", ".join(_PENDING_COLUMNS) + ") VALUES (?, 'slack', "
                   + ", ".join("?" * len(_PENDING_COLUMNS)) + ")",
                   (key, *(record.get(c, "" if c == "picker_ts" else None)
                           for c in _PENDING_COLUMNS)))
        if (slack / "_active_threads.json").is_file():
            retire.append(slack / "_active_threads.json")
    return counts, retire
