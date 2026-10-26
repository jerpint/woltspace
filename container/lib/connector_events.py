"""Which connector events the lodge has accepted, in lodge.db (connector_events).

The broker records an event before it answers with a receipt, so a resend
after a reconnect is a duplicate and is not dispatched twice.
"""

from __future__ import annotations

import time

import lodge_db

KEEP_SECONDS = 7 * 86400
_PRUNE_EVERY = 500
_count = 0


def record(wolts_dir, connector: str, event_id: str) -> bool:
    """True the first time this event is seen, False for a repeat."""
    global _count
    now = time.time()
    with lodge_db.write(wolts_dir) as conn:
        fresh = conn.execute(
            "INSERT OR IGNORE INTO connector_events (connector, event_id, ts) VALUES (?, ?, ?)",
            (connector, event_id, now),
        ).rowcount == 1
        _count += 1
        if _count % _PRUNE_EVERY == 0:
            conn.execute("DELETE FROM connector_events WHERE ts < ?", (now - KEEP_SECONDS,))
    return fresh
