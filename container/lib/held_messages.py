"""The one message a chat holds while its owner picks a wolt (lodge_messages, chat_lodge).

A row of the lodge DB's pending_messages per connector chat, id "<connector>:<chat>": the
verified sender, the original event id (the slot's generation, which its picker carries) and
the text the wolt will read (an upload's line keeps its saved path). Every function takes the
caller's open write connection, so a claim can commit in one transaction with the inbox
admission it feeds. Expiry is lazy, at the next hold or claim: no timer, so a restored copy
has nothing to disarm and replays nothing.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

TTL_SECONDS = 600
EXPIRED = "expired"


@dataclass(frozen=True)
class Held:
    id: str
    event_id: str
    text: str


def generation(event_id: str) -> str:
    """A short stand-in for the event id, small enough for any provider's button payload."""
    return hashlib.sha256(event_id.encode()).hexdigest()[:8]


def hold(conn, connector: str, chat, sender: str, event_id: str, text: str, now: float) -> str:
    """Hold `text` for this chat: "held"; EXPIRED, held in place of one that waited too long;
    or "waiting" when another message is already held (nothing changes)."""
    slot = f"{connector}:{chat}"
    row = conn.execute("SELECT event_id, state, expires_at FROM pending_messages WHERE id=?",
                       (slot,)).fetchone()
    live = row is not None and row["state"] == "pending"
    if live and row["expires_at"] > now:
        return "held" if row["event_id"] == event_id else "waiting"  # the same event resent is no second message
    conn.execute(
        "INSERT OR REPLACE INTO pending_messages (id, adapter, user, channel, root_ts, event_id, text,"
        " state, created_at, expires_at) VALUES (?, ?, ?, ?, '', ?, ?, 'pending', ?, ?)",
        (slot, connector, str(sender), str(chat), event_id, text, now, now + TTL_SECONDS))
    return EXPIRED if live else "held"


def waiting(conn, connector: str, chat, now: float) -> bool:
    """Whether a message is held in this chat, so another may not overtake it."""
    return conn.execute("SELECT 1 FROM pending_messages WHERE id=? AND state='pending' AND expires_at>?",
                        (f"{connector}:{chat}", now)).fetchone() is not None


def claim(conn, connector: str, chat, sender: str, gen: str | None, now: float):
    """The held message, for its own sender and, when a click names one, its picker's
    generation (a typed /wolt names none). Its text is cleared as it is taken, so it is handed
    over at most once. EXPIRED (cleared too) when it waited too long; None when nothing
    matching is held."""
    slot = f"{connector}:{chat}"
    row = conn.execute("SELECT event_id, text, user, expires_at FROM pending_messages"
                       " WHERE id=? AND state='pending'", (slot,)).fetchone()
    if row is None or row["user"] != str(sender) or gen is not None and generation(row["event_id"]) != gen:
        return None
    if row["expires_at"] <= now:
        conn.execute("UPDATE pending_messages SET state='expired', text=NULL WHERE id=?", (slot,))
        return EXPIRED
    conn.execute("UPDATE pending_messages SET state='claimed', text=NULL WHERE id=?", (slot,))
    return Held(slot, row["event_id"], row["text"])
