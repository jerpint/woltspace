"""Additional independent review regressions for shared held-message custody."""

import inbox
from inbox_support import limits
from test_held_message_negative import command, event, mail, pending, pick

pytest_plugins = ["test_held_message_negative"]


def test_refused_release_does_not_let_the_next_message_bypass_the_pending_slot(side, monkeypatch):
    event(side)
    with monkeypatch.context() as m:
        m.setattr(inbox, "LIMITS", limits(mailbox_bytes=1))
        pick(side)
    out = event(side, "second", body={"text": "second must wait"})
    assert mail(side) == [], "a refused first message must not be overtaken by a second admission"
    assert pending(side) == [("first", "first text")]
    assert "first message" in out.text.lower()


def test_holding_preserves_literal_uploads_marker_in_owner_text(side):
    text = "Please document the literal <uploads> placeholder."
    event(side, body={"text": text})
    command(side)
    assert text in mail(side)[0][1].decode()
