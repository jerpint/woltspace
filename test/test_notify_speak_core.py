"""Core has a message spoken once it knows where it goes, sends the audio as a voice note, and always cleans up."""

import json
import re
import stat
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

import outbox
from server import notify, speech
from server.notification_types import Attachment, Plugin

LINK = "https://lodge.test/tui?session=test-session"
OGG = b"OggS" + b"\x00" * 60
WORDS = {"text": "The build is green."}
# Named here and not inside a test: test_shadow_wolt.py reads the Telegram host
# in a test body as a live call. Nothing below leaves the process.
SEND_VOICE_URL = "https://api.telegram.org/bottelegram-token/sendVoice"


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
        "SLACK_BOT_TOKEN": "slack-token", "OPENAI_API_KEY": "sk-test",
    }
    state = SimpleNamespace(
        box=tmp_path / "outbox", env=env, sent=[], history=Mock(), routing=None,
        tmp=tmp_path, spoken=[], speech_error=None,
    )

    async def synthesize(text, speech_format, target, *, api_key, model, voice,
                         max_bytes=None, **_where):
        state.spoken.append(SimpleNamespace(
            text=text, speech_format=speech_format, target=target,
            api_key=api_key, model=model, voice=voice, max_bytes=max_bytes, **_where,
        ))
        if state.speech_error is not None:
            raise state.speech_error
        target.write_bytes(OGG)
        return len(OGG)

    (tmp_path / "tunnel.json").write_text('{"url": "https://lodge.test"}')
    monkeypatch.setattr(notify, "SPACE_PLATFORM_DIR", tmp_path)
    monkeypatch.setattr(notify, "OUTBOX_DIR", state.box)
    monkeypatch.setattr(notify, "WOLTS_DIR", tmp_path)  # woltspace.json `speech` lives here
    monkeypatch.setattr(notify, "dotenv_env", lambda key: env.get(key, ""))
    monkeypatch.setattr(notify, "read_session_registry", lambda name: state.routing)
    monkeypatch.setattr(notify, "append_chat_history", state.history)
    monkeypatch.setattr(notify, "SessionRegistry", lambda root: Mock())
    monkeypatch.setattr(notify, "synthesize", synthesize)
    # Recording senders, so these tests do not depend on a provider's transport.
    for adapter, secret, speech_format in (
        ("telegram", "TELEGRAM_BOT_TOKEN", "opus"), ("slack", "SLACK_BOT_TOKEN", "mp3"),
    ):
        monkeypatch.setitem(notify.SENDERS, adapter, Plugin(
            adapter, _recorder(state.sent, adapter), secrets=(secret,),
            max_attachment_bytes=50 * 1024 * 1024, speech_format=speech_format,
        ))
    return state


def _stage(lodge, name="weekly-update.html", data=b"<h1>hi</h1>") -> str:
    source = lodge.tmp / name
    source.write_bytes(data)
    return outbox.stage(source, lodge.box)


def _left_in_the_outbox(lodge) -> list:
    return sorted(lodge.box.iterdir()) if lodge.box.exists() else []


@pytest.mark.asyncio
async def test_the_words_are_spoken_and_sent_as_a_voice_attachment(lodge):
    result = await notify.send_notification(
        "test-session", "🦝 Bob: The build is green.\n", None, (), WORDS,
    )

    (spoken,) = lodge.spoken
    assert (spoken.text, spoken.speech_format) == ("The build is green.", "opus")
    assert (spoken.api_key, spoken.model, spoken.voice) == ("sk-test", "gpt-4o-mini-tts", "alloy")
    assert spoken.target.parent == lodge.box and re.fullmatch(r"[0-9a-f]{32}", spoken.target.name)
    (sent,) = lodge.sent
    assert sent.message.text == "🦝 Bob: The build is green.\n"
    assert sent.message.attachments == (
        Attachment(spoken.target, "voice-note.ogg", len(OGG), "audio/ogg", "voice"),
    )
    assert sent.content == [OGG]                 # still in the outbox while the sender runs
    assert _left_in_the_outbox(lodge) == []      # and gone afterwards
    assert stat.S_IMODE(lodge.box.stat().st_mode) == 0o700
    assert result == {
        "adapter": "telegram", "chat_id": "101",
        "attachments": [{
            "name": "voice-note.ogg", "content_type": "audio/ogg", "size": len(OGG),
            "kind": "voice",
        }],
    }
    lodge.history.assert_called_once_with(
        "telegram", "101",
        f"🦝 Bob: The build is green.\n\n[voice note sent: voice-note.ogg, audio/ogg, {len(OGG)} bytes]",
    )


@pytest.mark.asyncio
async def test_slack_gets_mp3(lodge):
    lodge.env["SLACK_NOTIFY_CHANNEL"] = "D0123456789"       # the lodge's own chat
    explicit = {"adapter": "slack", "channel": "D0123456789", "thread_ts": "1.2"}

    await notify.send_notification("test-session", "hi\n", explicit, (), WORDS)

    assert lodge.spoken[0].speech_format == "mp3"
    (attachment,) = lodge.sent[0].message.attachments
    assert (attachment.filename, attachment.content_type, attachment.kind) == (
        "voice-note.mp3", "audio/mpeg", "voice",
    )


@pytest.mark.asyncio
async def test_model_and_voice_come_from_the_lodge_settings(lodge):
    lodge.env.update(OPENAI_TTS_MODEL="tts-1", OPENAI_TTS_VOICE="nova")

    await notify.send_notification("test-session", "hi\n", None, (), WORDS)

    assert (lodge.spoken[0].model, lodge.spoken[0].voice) == ("tts-1", "nova")


@pytest.mark.asyncio
async def test_without_a_key_nothing_is_spoken_or_sent(lodge):
    del lodge.env["OPENAI_API_KEY"]

    with pytest.raises(notify.SpeechError) as error:
        await notify.send_notification("test-session", "hi\n", None, (), WORDS)

    assert error.value.reason == "speech_not_configured"
    assert "OPENAI_API_KEY" in str(error.value) and "send the message as text" in str(error.value)
    assert lodge.spoken == [] and lodge.sent == []
    lodge.history.assert_not_called()


@pytest.mark.asyncio
async def test_a_sender_that_declares_no_speech_format_refuses(lodge, monkeypatch):
    monkeypatch.setitem(notify.SENDERS, "telegram", Plugin(
        "telegram", _recorder(lodge.sent, "telegram"), secrets=("TELEGRAM_BOT_TOKEN",),
        max_attachment_bytes=50 * 1024 * 1024,
    ))

    with pytest.raises(notify.AttachmentError) as error:
        await notify.send_notification("test-session", "hi\n", None, (), WORDS)

    assert error.value.reason == "speech_unsupported"
    assert str(error.value) == "telegram cannot carry a spoken message"
    assert lodge.spoken == [] and lodge.sent == []


@pytest.mark.asyncio
async def test_a_sender_that_cannot_carry_the_audio_refuses_before_anything_is_spoken(
    lodge, monkeypatch,
):
    monkeypatch.setitem(notify.SENDERS, "telegram", Plugin(
        "telegram", _recorder(lodge.sent, "telegram"), secrets=("TELEGRAM_BOT_TOKEN",),
        speech_format="opus",
    ))

    with pytest.raises(notify.AttachmentError) as error:
        await notify.send_notification("test-session", "hi\n", None, (), WORDS)

    assert error.value.reason == "speech_unsupported"
    assert str(error.value) == "telegram cannot carry a spoken message"
    assert lodge.spoken == [] and lodge.sent == []


@pytest.mark.parametrize("speak", [
    "say this", ["text"], {}, {"text": 7}, {"text": ""}, {"text": "  \n"}, {"words": "hi"},
])
@pytest.mark.asyncio
async def test_a_malformed_speak_is_refused(lodge, speak):
    with pytest.raises(notify.AttachmentError) as error:
        await notify.send_notification("test-session", "hi\n", None, (), speak)

    assert error.value.reason == "speak_invalid"
    assert str(error.value) == "speak must be {text} with the words to say"
    assert lodge.spoken == [] and lodge.sent == []


@pytest.mark.asyncio
async def test_speech_and_a_file_together_are_refused_and_the_file_is_removed(lodge):
    entry = _stage(lodge)

    with pytest.raises(notify.AttachmentError) as error:
        await notify.send_notification(
            "test-session", "hi\n", None, [{"id": entry, "name": "weekly-update.html"}], WORDS,
        )

    assert error.value.reason == "speak_invalid"
    assert str(error.value) == "a spoken message cannot also carry a file"
    assert lodge.spoken == [] and lodge.sent == []
    assert _left_in_the_outbox(lodge) == []


@pytest.mark.asyncio
async def test_text_past_the_speech_limit_is_refused_and_the_limit_itself_is_spoken(lodge):
    with pytest.raises(notify.AttachmentError) as error:
        await notify.send_notification("test-session", "hi\n", None, (), {"text": "x" * 4097})

    assert error.value.reason == "speak_too_long"
    assert str(error.value) == "speech reads at most 4096 characters; this text has 4097"
    assert lodge.spoken == []

    await notify.send_notification("test-session", "hi\n", None, (), {"text": "x" * 4096})

    assert len(lodge.spoken[0].text) == 4096


@pytest.mark.asyncio
async def test_a_speech_failure_still_sends_the_words_as_text(lodge):
    """The service failing (after its retry) must not cost the human the message."""
    lodge.speech_error = notify.SpeechError("speech_failed", "could not reach the speech service: ReadTimeout")

    result = await notify.send_notification("test-session", "hi\n", None, (), WORDS)

    (sent,) = lodge.sent
    assert sent.message.attachments == ()
    assert sent.message.text == "hi\n\n(voice note failed; sent as text)"
    assert result["speech_failed"] == "could not reach the speech service: ReadTimeout"
    assert "attachments" not in result
    assert _left_in_the_outbox(lodge) == []


@pytest.mark.asyncio
async def test_a_speech_refusal_other_than_failure_still_sends_nothing(lodge):
    lodge.speech_error = notify.SpeechError("speech_too_large", "the spoken audio is over 5 bytes")

    with pytest.raises(notify.SpeechError, match="over 5 bytes"):
        await notify.send_notification("test-session", "hi\n", None, (), WORDS)

    assert lodge.sent == []
    lodge.history.assert_not_called()
    assert _left_in_the_outbox(lodge) == []


@pytest.mark.asyncio
async def test_the_audio_is_removed_when_the_sender_raises(lodge, monkeypatch):
    async def fail(message, destination, context):
        raise RuntimeError("Bad Request: VOICE_MESSAGES_FORBIDDEN")

    monkeypatch.setitem(notify.SENDERS, "telegram", Plugin(
        "telegram", fail, secrets=("TELEGRAM_BOT_TOKEN",),
        max_attachment_bytes=50 * 1024 * 1024, speech_format="opus",
    ))

    with pytest.raises(RuntimeError, match="VOICE_MESSAGES_FORBIDDEN"):
        await notify.send_notification("test-session", "hi\n", None, (), WORDS)

    assert len(lodge.spoken) == 1
    assert _left_in_the_outbox(lodge) == []
    lodge.history.assert_not_called()


@pytest.mark.asyncio
async def test_an_outbox_that_cannot_be_made_is_reported_without_its_path(lodge, monkeypatch):
    # tunnel.json is a file, so nothing can be made beneath it.
    monkeypatch.setattr(notify, "OUTBOX_DIR", lodge.tmp / "tunnel.json" / "outbox")

    with pytest.raises(RuntimeError) as error:
        await notify.send_notification("test-session", "hi\n", None, (), WORDS)

    assert not isinstance(error.value, notify.SpeechError)      # the route answers 500
    assert str(error.value) == "could not write the spoken audio: not a directory"
    assert lodge.spoken == [] and lodge.sent == []


@pytest.mark.asyncio
async def test_a_missing_token_is_reported_before_anything_is_spoken(lodge):
    lodge.env["TELEGRAM_BOT_TOKEN"] = ""
    explicit = {"adapter": "telegram", "chat_id": "101"}

    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN not set"):
        await notify.send_notification("test-session", "hi\n", explicit, (), WORDS)

    assert lodge.spoken == []


@pytest.mark.asyncio
async def test_a_slack_message_with_no_channel_is_reported_before_anything_is_spoken(lodge):
    explicit = {"adapter": "slack", "channel": "", "thread_ts": None}

    with pytest.raises(RuntimeError, match="no slack channel provided"):
        await notify.send_notification("test-session", "hi\n", explicit, (), WORDS)

    assert lodge.spoken == []


@pytest.mark.asyncio
async def test_no_notification_target_means_nothing_is_spoken(lodge):
    lodge.env.update(TELEGRAM_BOT_TOKEN="", TELEGRAM_ALLOWED_USERS="")

    with pytest.raises(notify.NoNotificationTarget):
        await notify.send_notification("test-session", "hi\n", None, (), WORDS)

    assert lodge.spoken == []


@pytest.mark.asyncio
async def test_the_session_s_own_route_is_spoken_to_as_well(lodge):
    lodge.routing = {"adapter": "slack", "chat_id": "D0123456789", "thread_ts": "1.2"}

    await notify.send_notification("test-session", "hi\n", None, (), WORDS)

    assert lodge.spoken[0].speech_format == "mp3"
    assert lodge.sent[0].destination == {"channel": "D0123456789", "thread_ts": "1.2"}
    assert lodge.sent[0].message.attachments[0].kind == "voice"


@pytest.mark.asyncio
async def test_calls_without_speak_never_reach_the_speech_client(lodge):
    entry = _stage(lodge)

    await notify.send_notification("test-session", "hi\n")
    await notify.send_notification(
        "test-session", "hi\n", None, [{"id": entry, "name": "weekly-update.html"}],
    )

    assert lodge.spoken == []
    assert [len(sent.message.attachments) for sent in lodge.sent] == [0, 1]


# --- both halves: the registered senders, and then the real speech client too ---

@pytest.fixture
def real_lodge(monkeypatch, tmp_path):
    """The registered senders, a key, and a speech client that writes Ogg. Returns the outbox."""
    env = {
        "TELEGRAM_BOT_TOKEN": "telegram-token", "TELEGRAM_ALLOWED_USERS": "101",
        "OPENAI_API_KEY": "sk-test",
    }

    async def synthesize(text, speech_format, target, *, api_key, model, voice,
                         max_bytes=None, **_where):
        target.write_bytes(OGG)
        return len(OGG)

    (tmp_path / "tunnel.json").write_text('{"url": "https://lodge.test"}')
    monkeypatch.setattr(notify, "SPACE_PLATFORM_DIR", tmp_path)
    monkeypatch.setattr(notify, "OUTBOX_DIR", tmp_path / "outbox")
    monkeypatch.setattr(notify, "dotenv_env", lambda key: env.get(key, ""))
    monkeypatch.setattr(notify, "read_session_registry", lambda name: None)
    monkeypatch.setattr(notify, "append_chat_history", Mock())
    monkeypatch.setattr(notify, "SessionRegistry", lambda root: Mock())
    monkeypatch.setattr(notify, "synthesize", synthesize)
    return tmp_path / "outbox"


@pytest.mark.asyncio
async def test_end_to_end_a_spoken_message_goes_out_through_send_voice(real_lodge, monkeypatch):
    heard = []
    text, send_document = AsyncMock(), AsyncMock()
    send_voice = AsyncMock(side_effect=lambda token, chat_id, attachment, caption: heard.append(
        attachment.path.read_bytes(),
    ))
    monkeypatch.setattr(notify, "telegram_send", text)
    monkeypatch.setattr(notify, "telegram_send_document", send_document)
    monkeypatch.setattr(notify, "telegram_send_voice", send_voice)

    result = await notify.send_notification(
        "test-session", "🦝 Bob: The build is green.\n", None, (), {"text": "The build is green."},
    )

    token, chat_id, attachment, caption = send_voice.await_args.args
    assert (token, chat_id) == ("telegram-token", "101")
    assert (attachment.filename, attachment.content_type, attachment.kind) == (
        "voice-note.ogg", "audio/ogg", "voice",
    )
    assert heard == [OGG]
    assert caption.startswith("🦝 Bob: The build is green.\n\n\n---")
    send_document.assert_not_awaited()
    text.assert_not_awaited()
    assert result["attachments"] == [
        {"name": "voice-note.ogg", "content_type": "audio/ogg", "size": len(OGG), "kind": "voice"},
    ]
    assert list(real_lodge.iterdir()) == []


@pytest.mark.asyncio
async def test_end_to_end_the_service_s_audio_is_what_telegram_is_handed(real_lodge, monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        if str(request.url) == speech.SPEECH_URL:
            return httpx.Response(200, content=OGG)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    class Client(httpx.AsyncClient):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    # The speech client and the Telegram transport are both the real ones here;
    # only the network under them is a stand-in.
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    monkeypatch.setattr(notify, "synthesize", speech.synthesize)

    result = await notify.send_notification(
        "test-session", "🦝 Bob: The build is green.\n", None, (), {"text": "The build is green."},
    )

    said, sent = requests
    assert said.headers["authorization"] == "Bearer sk-test"
    assert json.loads(said.content) == {
        "model": "gpt-4o-mini-tts", "voice": "alloy",
        "input": "The build is green.", "response_format": "opus",
    }
    assert str(sent.url) == SEND_VOICE_URL
    assert b'name="voice"; filename="voice-note.ogg"' in sent.content
    assert OGG in sent.content
    assert "🦝 Bob: The build is green.".encode() in sent.content
    assert result["attachments"][0]["kind"] == "voice"
    assert list(real_lodge.iterdir()) == []
