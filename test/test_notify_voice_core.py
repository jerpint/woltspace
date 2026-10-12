"""Core turns an outbox entry marked voice into a voice attachment, and refuses what cannot be one."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import outbox
from server import notify
from server.notification_types import Attachment, OutboundMessage, Plugin

LINK = "https://lodge.test/tui?session=test-session"
OGG = b"OggS" + b"\x00" * 23


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


def _voice(entry: str, name: str = "note.ogg") -> list[dict]:
    return [{"id": entry, "name": name, "kind": "voice"}]


@pytest.mark.asyncio
async def test_a_voice_entry_reaches_the_sender_as_a_voice_attachment(lodge):
    entry = _stage(lodge, "note.ogg", OGG)

    result = await notify.send_notification("test-session", "hi\n", None, _voice(entry))

    (sent,) = lodge.sent
    assert sent.message.attachments == (
        Attachment(lodge.box / entry, "note.ogg", len(OGG), "audio/ogg", "voice"),
    )
    assert sent.content == [OGG]
    assert result == {
        "adapter": "telegram", "chat_id": "101",
        "attachments": [
            {"name": "note.ogg", "content_type": "audio/ogg", "size": len(OGG), "kind": "voice"},
        ],
    }
    lodge.history.assert_called_once_with(
        "telegram", "101", f"hi\n\n[voice note sent: note.ogg, audio/ogg, {len(OGG)} bytes]",
    )
    assert not (lodge.box / entry).exists()


@pytest.mark.parametrize("name, content_type", [
    ("note.oga", "audio/ogg"), ("note.opus", "audio/ogg"), ("NOTE.MP3", "audio/mpeg"),
    ("memo.m4a", "audio/mp4"),
])
@pytest.mark.asyncio
async def test_the_voice_type_is_read_from_the_name(lodge, name, content_type):
    entry = _stage(lodge, name, OGG)

    await notify.send_notification("test-session", "hi\n", None, _voice(entry, name))

    (attachment,) = lodge.sent[0].message.attachments
    assert (attachment.content_type, attachment.kind) == (content_type, "voice")


@pytest.mark.parametrize("name", ["notes.wav", "page.html", "note", "note.ogg.txt"])
@pytest.mark.asyncio
async def test_a_name_that_cannot_be_a_voice_note_is_refused_and_removed(lodge, name):
    entry = _stage(lodge, name, OGG)

    with pytest.raises(notify.AttachmentError) as error:
        await notify.send_notification("test-session", "hi\n", None, _voice(entry, name))

    assert error.value.reason == "voice_format_unsupported"
    assert str(error.value) == (
        f"{name} cannot be a voice note; send audio ending in .ogg, .oga, .opus, .mp3 or .m4a"
    )
    assert lodge.sent == []
    lodge.history.assert_not_called()
    assert not (lodge.box / entry).exists()


@pytest.mark.parametrize("kind", ["video", "", 7, None, ["voice"]])
@pytest.mark.asyncio
async def test_an_unknown_kind_is_refused(lodge, kind):
    entry = _stage(lodge, "note.ogg", OGG)

    with pytest.raises(notify.AttachmentError) as error:
        await notify.send_notification(
            "test-session", "hi\n", None, [{"id": entry, "name": "note.ogg", "kind": kind}],
        )

    assert error.value.reason == "attachment_invalid"
    assert str(error.value) == 'attachment kind must be "file" or "voice"'
    assert lodge.sent == []
    assert not (lodge.box / entry).exists()


@pytest.mark.asyncio
async def test_kind_file_is_the_same_as_no_kind(lodge):
    entry = _stage(lodge)

    result = await notify.send_notification(
        "test-session", "hi\n", None,
        [{"id": entry, "name": "weekly-update.html", "kind": "file"}],
    )

    assert lodge.sent[0].message.attachments == (
        Attachment(lodge.box / entry, "weekly-update.html", 11, "text/html"),
    )
    assert result["attachments"] == [
        {"name": "weekly-update.html", "content_type": "text/html", "size": 11},
    ]
    lodge.history.assert_called_once_with(
        "telegram", "101", "hi\n\n[file sent: weekly-update.html, text/html, 11 bytes]",
    )


@pytest.mark.asyncio
async def test_an_audio_file_sent_as_a_file_stays_a_document(lodge):
    entry = _stage(lodge, "note.ogg", OGG)

    await notify.send_notification("test-session", "hi\n", None, [{"id": entry, "name": "note.ogg"}])

    assert lodge.sent[0].message.attachments[0].kind == "file"


@pytest.mark.asyncio
async def test_a_voice_entry_sent_to_slack_is_still_answered_as_a_voice_note(lodge):
    # `notify --voice` reads the mark in the answer; without it the command
    # would report a Slack send as one an older lodge made.
    entry = _stage(lodge, "note.mp3", OGG)
    explicit = {"adapter": "slack", "channel": "D0123456789", "thread_ts": "1.2"}

    result = await notify.send_notification(
        "test-session", "hi\n", explicit, _voice(entry, "note.mp3"),
    )

    (sent,) = lodge.sent
    assert sent.adapter == "slack"
    assert sent.message.attachments == (
        Attachment(lodge.box / entry, "note.mp3", len(OGG), "audio/mpeg", "voice"),
    )
    assert result == {
        "adapter": "slack", "channel": "D0123456789",
        "attachments": [
            {"name": "note.mp3", "content_type": "audio/mpeg", "size": len(OGG), "kind": "voice"},
        ],
    }
    lodge.history.assert_called_once_with(
        "slack", "D0123456789",
        f"hi\n\n[voice note sent: note.mp3, audio/mpeg, {len(OGG)} bytes]",
    )


@pytest.mark.asyncio
async def test_slack_carries_a_voice_attachment_as_an_ordinary_upload(tmp_path):
    from server.notification_senders import slack_sender
    from server.notification_types import SendContext

    path = tmp_path / ("b" * 32)
    path.write_bytes(OGG)
    voice = Attachment(path, "note.ogg", len(OGG), "audio/ogg", "voice")
    text, set_status, upload = AsyncMock(), AsyncMock(), AsyncMock()
    send = slack_sender(text, set_status, upload)

    await send(
        OutboundMessage("hi", None, (voice,)),
        {"channel": "D0123456789", "thread_ts": "1.2"},
        SendContext({"SLACK_BOT_TOKEN": "x"}, {}),
    )

    upload.assert_awaited_once_with("x", "D0123456789", "1.2", voice, "hi")
    text.assert_not_awaited()


# --- the whole path, through the registered Telegram sender -------------------

@pytest.fixture
def real_senders(monkeypatch, tmp_path):
    env = {"TELEGRAM_BOT_TOKEN": "telegram-token", "TELEGRAM_ALLOWED_USERS": "101"}
    (tmp_path / "tunnel.json").write_text('{"url": "https://lodge.test"}')
    monkeypatch.setattr(notify, "SPACE_PLATFORM_DIR", tmp_path)
    monkeypatch.setattr(notify, "OUTBOX_DIR", tmp_path / "outbox")
    monkeypatch.setattr(notify, "dotenv_env", lambda key: env.get(key, ""))
    monkeypatch.setattr(notify, "read_session_registry", lambda name: None)
    monkeypatch.setattr(notify, "append_chat_history", Mock())
    monkeypatch.setattr(notify, "SessionRegistry", lambda root: Mock())
    source = tmp_path / "note.ogg"
    source.write_bytes(OGG)
    return tmp_path / "outbox", outbox.stage(source, tmp_path / "outbox")


@pytest.mark.asyncio
async def test_end_to_end_a_voice_entry_goes_out_through_send_voice(real_senders, monkeypatch):
    box, entry = real_senders
    text, send_document, send_voice = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(notify, "telegram_send", text)
    monkeypatch.setattr(notify, "telegram_send_document", send_document)
    monkeypatch.setattr(notify, "telegram_send_voice", send_voice)

    await notify.send_notification("test-session", "hi\n", None, _voice(entry))

    token, chat_id, attachment, caption = send_voice.await_args.args
    assert (token, chat_id) == ("telegram-token", "101")
    assert attachment == Attachment(box / entry, "note.ogg", len(OGG), "audio/ogg", "voice")
    assert caption.startswith("hi\n\n\n---")     # the text's own newline, then the footer's two
    send_document.assert_not_awaited()
    text.assert_not_awaited()
    assert not (box / entry).exists()
