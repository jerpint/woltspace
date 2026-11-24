"""Record the conversation UUID reported by one Antigravity TUI process."""

import json
import os
import sqlite3
import sys
from uuid import UUID


def capture_id(name: str, payload: object) -> None:
    if not isinstance(payload, dict):
        return
    # agy can allocate an ID before first-run setup has left the wizard.
    if payload.get("agent_state") in ("authenticating", "initializing"):
        return
    conversation_id = payload.get("conversation_id")
    cwd = payload.get("cwd")
    if not isinstance(conversation_id, str) or not isinstance(cwd, str) or not cwd:
        return
    try:
        if str(UUID(conversation_id)) != conversation_id:
            return
    except ValueError:
        return

    from sessions import SessionRegistry, stamp_resume_id, stored_resume_id

    registry = SessionRegistry()
    record = registry.get(name, check_alive=False)
    if (
        not record
        or record.get("harness") != "antigravity"
        or stored_resume_id(record)
        or os.path.realpath(cwd) != os.path.realpath(record.get("dir") or "")
    ):
        return

    try:
        # First report wins. An interactive /new or /resume can change the
        # active conversation, but cannot rebind the Woltspace session.
        stamp_resume_id(registry, record["wolt"], name, conversation_id)
    except sqlite3.IntegrityError:
        # The DB also refuses an ID owned by a different Woltspace session.
        return


if __name__ == "__main__":
    try:
        capture_id(sys.argv[1], json.load(sys.stdin))
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
