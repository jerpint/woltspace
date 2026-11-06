"""Independent black-box regressions for the shared hold-while-picking contract.

Exercise real pending/inbox SQLite custody via ChatLodge.handle. Only runtime
edges are faked; no dependency on the proposed held_messages helper API.
"""

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from test.inbox_support import limits, rows, wolt

ROOT = Path(__file__).resolve().parents[1]
for part in ("container", "container/lib"):
    sys.path.insert(0, str(ROOT / part))

import held_messages  # noqa: E402
import inbox  # noqa: E402
import lodge_db  # noqa: E402

OWNER, OTHER, CHAT = "111", "222", "4242"


class Out:
    def __init__(self):
        self.replies = []
        self.acks = []

    async def say(self, reply, photo=None):
        self.replies.append(reply)

    async def edit(self, reply):
        self.replies.append(reply)

    async def ack(self, ack):
        self.acks.append(ack)

    @property
    def text(self):
        return "\n".join(r.text for r in self.replies)


@pytest.fixture
def side(tmp_path, monkeypatch):
    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(tmp_path))
    import chat_lodge
    import connector_registry

    for name in ("alpha", "beta"):
        wolt(tmp_path, name)
    (tmp_path / "woltspace.json").write_text(json.dumps({"inbox": {"enabled": True}}))
    calls = []

    def wake(root, status):
        assert not lodge_db.in_write(), "wake must run after commit"
        assert rows(root, "SELECT key FROM inbox_messages WHERE seq=?", status.seq)
        calls.append(status)

    def unexpected(*args, **kwargs):
        pytest.fail("inbox-on hold must not spawn or paste")

    for name, value in (
        ("dog_available", lambda: False), ("get_tunnel_url", lambda: ""),
        ("_bot_log", lambda *a: None), ("notify_admitted", wake),
        ("message_session", unexpected), ("start_claude_session", unexpected),
        # bot.core reads the wolts dir it was first imported with
        ("list_wolts", lambda: [{"name": "alpha", "type": "beaver"}, {"name": "beta", "type": "beaver"}]),
    ):
        monkeypatch.setattr(chat_lodge, name, value)
    lodge: Any = chat_lodge.ChatLodge(connector_registry.spec("telegram"), [OWNER, OTHER])
    lodge.root, lodge.wakes = tmp_path, calls
    return lodge


def event(side, key="first", *, kind="message", sender=OWNER, chat=CHAT, body=None):
    out = Out()
    asyncio.run(side.handle({"id": key, "sender": sender, "kind": kind,
                            "route": {"chat_id": chat},
                            "body": body if body is not None else {"text": "first text"}}, out))
    return out


def pick(side, generation="first", *, sender=OWNER, chat=CHAT):
    return event(side, f"click-{generation}", kind="selection", sender=sender, chat=chat,
                 body={"action": f"hold:alpha:{held_messages.generation(generation)}"})  # the picker carries a short generation


def command(side, target="alpha", sender=OWNER):
    return event(side, "command", kind="command", sender=sender,
                 body={"name": "wolt", "args": [target]})


def pending(side):
    return rows(side.root, "SELECT event_id, text FROM pending_messages WHERE state='pending'")


def mail(side):
    return rows(side.root, "SELECT key, body FROM inbox_messages ORDER BY seq")


def test_second_event_and_retry_cannot_overwrite_the_original(side):
    event(side)
    refused = event(side, "second", body={"text": "must not replace first"})
    event(side)  # retry of the original provider event
    command(side)
    assert [key for key, _ in mail(side)] == ["telegram:first"]
    assert "first text" in mail(side)[0][1].decode()
    assert "must not replace first" not in mail(side)[0][1].decode()
    assert "first message" in refused.text.lower()
    assert not pending(side)


def test_another_authorized_sender_cannot_release_the_slot(side):
    event(side)
    command(side, sender=OTHER)
    pick(side, sender=OTHER)
    assert mail(side) == []
    assert pending(side) == [("first", "first text")]
    pick(side)
    assert [key for key, _ in mail(side)] == ["telegram:first"]


def test_a_generation_from_another_chat_cannot_consume_a_hold(side):
    event(side)
    pick(side, chat="5252")
    assert mail(side) == []
    assert pending(side) == [("first", "first text")]
    pick(side)
    assert len(mail(side)) == 1


def test_expired_picker_cannot_consume_a_replacement(side):
    event(side)
    with lodge_db.write(side.root) as conn:
        conn.execute("UPDATE pending_messages SET expires_at=0")
    event(side, "replacement", body={"text": "replacement text"})
    pick(side)
    assert mail(side) == []
    assert pending(side) == [("replacement", "replacement text")]
    pick(side, "replacement")
    assert [key for key, _ in mail(side)] == ["telegram:replacement"]


def test_refused_admission_rolls_back_claim_then_same_picker_can_retry(side, monkeypatch):
    event(side)
    with monkeypatch.context() as m:
        m.setattr(inbox, "LIMITS", limits(mailbox_bytes=1))  # every body is too big: refused
        refused = pick(side)
    assert mail(side) == []
    assert side.wakes == []
    assert pending(side) == [("first", "first text")]
    assert "not queued" in refused.text.lower() and "held" in refused.text.lower()
    pick(side)
    pick(side)  # double click must not admit a second message
    assert [key for key, _ in mail(side)] == ["telegram:first"]
    assert not pending(side)


def test_hold_release_never_uses_the_nested_admit_writer(side, monkeypatch):
    event(side)

    def forbidden(*args, **kwargs):
        pytest.fail("held release must use admit_within in the claim transaction")

    monkeypatch.setattr(inbox.Store, "admit", forbidden)
    command(side)
    assert [key for key, _ in mail(side)] == ["telegram:first"]
    assert len(side.wakes) == 1


def test_unknown_target_does_not_consume_the_held_message(side):
    event(side)
    command(side, target="missing")
    assert mail(side) == []
    assert pending(side) == [("first", "first text")]
    command(side)
    assert len(mail(side)) == 1


def test_backup_restore_keeps_attachment_passive_and_delivers_a_restored_path(side, monkeypatch):
    import chat_lodge

    from woltspace.backup import create_backup, restore_backup

    upload = side.uploads_dir / "report.pdf"
    upload.parent.mkdir(parents=True)
    upload.write_bytes(b"dummy attachment")
    event(side, kind="attachment", body={"media": "file", "path": str(upload),
                                        "name": "report.pdf", "mime": "application/pdf", "size": 16})
    archive = create_backup(side.root, out_dir=side.root.parent / "snapshots", tag="held")
    restored = restore_backup(archive.archive, to=side.root.parent / "restored")
    upload.unlink()  # original upload is unavailable on the restored machine
    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(restored.wolts_dir))
    copy: Any = chat_lodge.ChatLodge(side.spec, [OWNER, OTHER])
    copy.root, copy.wakes = restored.wolts_dir, side.wakes
    assert mail(copy) == [] and copy.wakes == []  # no replay at restore/init
    assert (copy.uploads_dir / "report.pdf").read_bytes() == b"dummy attachment"
    pick(copy, "stale")
    assert mail(copy) == []
    pick(copy)
    [(key, body)] = mail(copy)
    assert key == "telegram:first"
    assert str(copy.uploads_dir / "report.pdf") in body.decode()
    assert str(upload) not in body.decode()
