"""Core turns outbox ids into attachments, refuses what cannot go, and always cleans up."""

import os
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import outbox
from server import notify
from server.notification_types import Attachment, OutboundMessage, Plugin

LINK = "https://lodge.test/tui?session=test-session"
INVALID = "attachments must be a list with at most one {id, name} entry"


def _recorder(sent, adapter):
    async def send(message, destination, context):
        # Read at send time: the entry must still be in the outbox here.
        content = [attachment.path.read_bytes() for attachment in message.attachments]
        sent.append(SimpleNamespace(
            adapter=adapter, message=message, destination=dict(destination), content=content,
        ))
    return send


@pytest.fixture
def lodge(monkeypatch, tmp_path):
    env = {
        "TELEGRAM_BOT_TOKEN": "telegram-token", "TELEGRAM_ALLOWED_USERS": "101",
        "SLACK_BOT_TOKEN": "slack-token",
    }
    state = SimpleNamespace(
        box=tmp_path / "outbox", env=env, sent=[], history=Mock(), routing=None, tmp=tmp_path,
    )
    (tmp_path / "tunnel.json").write_text('{"url": "https://lodge.test"}')
    monkeypatch.setattr(notify, "SPACE_PLATFORM_DIR", tmp_path)
    monkeypatch.setattr(notify, "OUTBOX_DIR", state.box)
    monkeypatch.setattr(notify, "dotenv_env", lambda key: env.get(key, ""))
    monkeypatch.setattr(notify, "read_session_registry", lambda name: state.routing)
    monkeypatch.setattr(notify, "append_chat_history", state.history)
    monkeypatch.setattr(notify, "SessionRegistry", lambda root: Mock())
    # Recording senders, so these tests do not depend on a provider's transport.
    for adapter, secret in (("telegram", "TELEGRAM_BOT_TOKEN"), ("slack", "SLACK_BOT_TOKEN")):
        monkeypatch.setitem(notify.SENDERS, adapter, Plugin(
            adapter, _recorder(state.sent, adapter), secrets=(secret,),
            max_attachment_bytes=50 * 1024 * 1024,
        ))
    return state


def _stage(lodge, name="weekly-update.html", data=b"<h1>hi</h1>") -> str:
    source = lodge.tmp / name
    source.write_bytes(data)
    return outbox.stage(source, lodge.box)


async def _refused(lodge, attachments) -> notify.AttachmentError:
    with pytest.raises(notify.AttachmentError) as error:
        await notify.send_notification("test-session", "hi\n", None, attachments)
    assert lodge.sent == []
    lodge.history.assert_not_called()
    return error.value


@pytest.mark.asyncio
async def test_attachment_reaches_the_sender_and_the_entry_is_removed(lodge):
    entry = _stage(lodge)

    result = await notify.send_notification(
        "test-session", "hi\n", None, [{"id": entry, "name": "weekly-update.html"}],
    )

    (sent,) = lodge.sent
    assert sent.message == OutboundMessage("hi\n", LINK, (
        Attachment(lodge.box / entry, "weekly-update.html", 11, "text/html"),
    ))
    assert sent.content == [b"<h1>hi</h1>"]
    assert sent.destination == {"chat_id": "101"}
    assert result == {
        "adapter": "telegram", "chat_id": "101",
        "attachments": [{"name": "weekly-update.html", "content_type": "text/html", "size": 11}],
    }
    lodge.history.assert_called_once_with(
        "telegram", "101", "hi\n\n[file sent: weekly-update.html, text/html, 11 bytes]",
    )
    assert not (lodge.box / entry).exists()


@pytest.mark.asyncio
async def test_entry_is_removed_when_the_sender_raises(lodge, monkeypatch):
    original = RuntimeError("provider error")

    async def fail(message, destination, context):
        raise original

    monkeypatch.setitem(notify.SENDERS, "telegram", Plugin(
        "telegram", fail, secrets=("TELEGRAM_BOT_TOKEN",), max_attachment_bytes=1024,
    ))
    entry = _stage(lodge)

    with pytest.raises(RuntimeError) as error:
        await notify.send_notification("test-session", "hi\n", None, [{"id": entry, "name": "a.html"}])

    assert error.value is original
    lodge.history.assert_not_called()
    assert not (lodge.box / entry).exists()


@pytest.mark.asyncio
async def test_entry_is_removed_when_there_is_no_notification_target(lodge):
    lodge.env["TELEGRAM_ALLOWED_USERS"] = ""
    entry = _stage(lodge)

    with pytest.raises(notify.NoNotificationTarget):
        await notify.send_notification("test-session", "hi\n", None, [{"id": entry, "name": "a.html"}])

    assert lodge.sent == []
    assert not (lodge.box / entry).exists()


@pytest.mark.asyncio
async def test_missing_entry_is_attachment_not_found_and_nothing_is_sent(lodge):
    error = await _refused(lodge, [{"id": "a" * 32, "name": "a.html"}])

    assert error.reason == "attachment_not_found"
    assert str(error) == "attachment is no longer in the outbox"


@pytest.mark.asyncio
async def test_symlink_entry_is_attachment_invalid_and_nothing_is_sent(lodge):
    secret = lodge.tmp / "secret"
    secret.write_text("s3cret")
    lodge.box.mkdir()
    (lodge.box / ("a" * 32)).symlink_to(secret)

    error = await _refused(lodge, [{"id": "a" * 32, "name": "a.html"}])

    assert error.reason == "attachment_invalid"
    assert str(error) == "attachment is not a regular file"
    assert secret.read_text() == "s3cret"                   # only the link is removed
    assert not (lodge.box / ("a" * 32)).is_symlink()


@pytest.mark.asyncio
async def test_sender_without_file_support_refuses(lodge, monkeypatch):
    monkeypatch.setitem(notify.SENDERS, "telegram", Plugin(
        "telegram", _recorder(lodge.sent, "telegram"), secrets=("TELEGRAM_BOT_TOKEN",),
    ))
    entry = _stage(lodge)

    error = await _refused(lodge, [{"id": entry, "name": "weekly-update.html"}])

    assert error.reason == "attachments_unsupported"
    assert str(error) == "telegram cannot carry files"
    assert not (lodge.box / entry).exists()


@pytest.mark.asyncio
async def test_oversized_file_refuses(lodge, monkeypatch):
    monkeypatch.setitem(notify.SENDERS, "telegram", Plugin(
        "telegram", _recorder(lodge.sent, "telegram"), secrets=("TELEGRAM_BOT_TOKEN",),
        max_attachment_bytes=5,
    ))
    entry = _stage(lodge)

    error = await _refused(lodge, [{"id": entry, "name": "weekly-update.html"}])

    assert error.reason == "attachment_too_large"
    assert str(error) == "weekly-update.html is 11 bytes; telegram accepts at most 5"
    assert not (lodge.box / entry).exists()


@pytest.mark.asyncio
async def test_file_of_exactly_the_limit_is_sent(lodge, monkeypatch):
    monkeypatch.setitem(notify.SENDERS, "telegram", Plugin(
        "telegram", _recorder(lodge.sent, "telegram"), secrets=("TELEGRAM_BOT_TOKEN",),
        max_attachment_bytes=11,
    ))
    entry = _stage(lodge)

    await notify.send_notification("test-session", "hi\n", None, [{"id": entry, "name": "a.html"}])

    assert [sent.content for sent in lodge.sent] == [[b"<h1>hi</h1>"]]


@pytest.mark.asyncio
async def test_a_missing_token_is_reported_before_the_file(lodge, monkeypatch):
    # A lodge that cannot send at all says so first, whatever the file's fate would be.
    monkeypatch.setitem(notify.SENDERS, "telegram", Plugin(
        "telegram", _recorder(lodge.sent, "telegram"), secrets=("TELEGRAM_BOT_TOKEN",),
    ))
    lodge.env["TELEGRAM_BOT_TOKEN"] = ""
    entry = _stage(lodge)

    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN not set"):
        await notify.send_notification(
            "test-session", "hi\n", {"adapter": "telegram", "chat_id": "404"},
            [{"id": entry, "name": "a.html"}],
        )

    assert lodge.sent == []
    assert not (lodge.box / entry).exists()


@pytest.mark.asyncio
async def test_two_attachments_are_refused(lodge):
    first, second = _stage(lodge, "a.html"), _stage(lodge, "b.html")

    error = await _refused(lodge, [{"id": first, "name": "a.html"}, {"id": second, "name": "b.html"}])

    assert error.reason == "attachment_invalid"
    assert str(error) == INVALID
    assert list(lodge.box.iterdir()) == []                  # both entries are cleaned up


@pytest.mark.asyncio
@pytest.mark.parametrize("attachments", [
    "a" * 32, {"id": "a" * 32, "name": "x"}, [{"name": "x"}], [{"id": 5}], ["a" * 32], 7, None,
])
async def test_malformed_attachments_are_refused(lodge, attachments):
    error = await _refused(lodge, attachments)

    assert error.reason == "attachment_invalid"
    assert str(error) == INVALID


@pytest.mark.asyncio
@pytest.mark.parametrize("name, filename, content_type", [
    ("data.zzz", "data.zzz", "application/octet-stream"),
    ("../../etc/passwd", "passwd", "application/octet-stream"),
    ("résumé \U0001F99D.pdf", "résumé \U0001F99D.pdf", "application/pdf"),
    ("bad\x00na\nme.txt", "badname.txt", "text/plain"),
    (None, "file", "application/octet-stream"),
    (7, "file", "application/octet-stream"),
])
async def test_unknown_content_type_falls_back_to_octet_stream(lodge, name, filename, content_type):
    entry = _stage(lodge)

    result = await notify.send_notification("test-session", "hi\n", None, [{"id": entry, "name": name}])

    (attachment,) = lodge.sent[0].message.attachments
    assert (attachment.filename, attachment.content_type) == (filename, content_type)
    assert attachment.path == lodge.box / entry             # the name never picks the file
    assert result["attachments"] == [{"name": filename, "content_type": content_type, "size": 11}]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [
    "explicit_telegram", "explicit_slack", "session_telegram", "session_slack", "default",
])
async def test_every_route_carries_the_attachment(lodge, case):
    explicit, adapter, destination = None, "telegram", {"chat_id": "101"}
    if case == "explicit_telegram":
        explicit, destination = {"adapter": "telegram", "chat_id": 404}, {"chat_id": "404"}
    elif case == "explicit_slack":
        explicit = {"adapter": "slack", "channel": "C123", "thread_ts": "1.2"}
        adapter, destination = "slack", {"channel": "C123", "thread_ts": "1.2"}
    elif case == "session_telegram":
        lodge.routing = {"adapter": "telegram", "chat_id": 303, "wolt": "testwolt"}
        destination = {"chat_id": "303"}
    elif case == "session_slack":
        lodge.routing = {"adapter": "slack", "chat_id": "C123", "thread_ts": "1.2", "wolt": "testwolt"}
        adapter, destination = "slack", {"channel": "C123", "thread_ts": "1.2"}
    entry = _stage(lodge)

    result = await notify.send_notification(
        "test-session", "hi\n", explicit, [{"id": entry, "name": "a.html"}],
    )

    (sent,) = lodge.sent
    assert (sent.adapter, sent.destination) == (adapter, destination)
    assert sent.content == [b"<h1>hi</h1>"]
    assert result["attachments"] == [{"name": "a.html", "content_type": "text/html", "size": 11}]
    assert not (lodge.box / entry).exists()


@pytest.mark.asyncio
async def test_text_only_call_is_unchanged(lodge):
    result = await notify.send_notification("test-session", "hi\n")

    assert result == {"adapter": "telegram", "chat_id": "101"}
    assert lodge.sent[0].message == OutboundMessage("hi\n", LINK)
    lodge.history.assert_called_once_with("telegram", "101", "hi\n")


@pytest.mark.asyncio
async def test_every_call_sweeps_stale_entries(lodge):
    stale, fresh = _stage(lodge, "a.html"), _stage(lodge, "b.html")
    two_hours_ago = time.time() - 7200
    os.utime(lodge.box / stale, (two_hours_ago, two_hours_ago))

    await notify.send_notification("test-session", "hi\n")

    assert [path.name for path in lodge.box.iterdir()] == [fresh]


@pytest.mark.asyncio
async def test_a_failing_sweep_does_not_stop_the_notification(lodge, monkeypatch):
    monkeypatch.setattr(notify.outbox, "sweep", Mock(side_effect=OSError("disk on fire")))

    result = await notify.send_notification("test-session", "hi\n")

    assert result == {"adapter": "telegram", "chat_id": "101"}
    assert len(lodge.sent) == 1
