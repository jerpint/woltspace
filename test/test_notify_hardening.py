"""Hardening on top of notify --file and --speak: what the outbox refuses, a
cap on spoken audio while it downloads, and no lodge paths in errors."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container"))

import outbox
from server import notify, speech
from server.notification_types import Plugin
from server.speech import SpeechError, synthesize

OGG = b"OggS" + b"\x00" * 60
SETTINGS = {"api_key": "sk-test", "model": "gpt-4o-mini-tts", "voice": "alloy"}


@pytest.mark.parametrize("path, rule", [
    ("/home/me/.aws/credentials", "aws-credentials"),
    ("/home/me/.aws/config", "aws-credentials"),
    ("/home/me/Passwords.KDBX", "password-vault"),
    ("/lodge/wolts/.space/logs/bot.jsonl", "lodge-state"),
    ("/lodge/wolts/.space/platform/tunnel.json", "lodge-state"),
])
def test_more_credential_paths_are_refused(path, rule):
    assert outbox.credential_rule(Path(path)) == rule


@pytest.mark.parametrize("path", [
    "/lodge/wolts/softie/wolt/sparks/report.pdf",
    "/home/me/aws/config",            # not the .aws folder
    "/lodge/wolts/softie/space/notes.md",
])
def test_ordinary_files_still_go(path):
    assert outbox.credential_rule(Path(path)) is None


def _service(monkeypatch, body):
    def handler(request):
        return httpx.Response(200, content=body)

    class Client(httpx.AsyncClient):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(speech.httpx, "AsyncClient", Client)


@pytest.mark.asyncio
async def test_spoken_audio_past_the_cap_stops_and_leaves_no_file(monkeypatch, tmp_path):
    _service(monkeypatch, OGG + b"\x00" * 4096)
    target = tmp_path / "entry"

    with pytest.raises(SpeechError) as error:
        await synthesize("hi", "opus", target, max_bytes=1024, **SETTINGS)

    assert error.value.reason == "speech_too_large"
    assert not target.exists()


@pytest.mark.asyncio
async def test_spoken_audio_under_the_cap_is_kept(monkeypatch, tmp_path):
    _service(monkeypatch, OGG)
    target = tmp_path / "entry"

    assert await synthesize("hi", "opus", target, max_bytes=1024, **SETTINGS) == len(OGG)
    assert target.read_bytes() == OGG


@pytest.fixture
def lodge(monkeypatch, tmp_path):
    env = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_ALLOWED_USERS": "101", "OPENAI_API_KEY": "sk"}
    state = SimpleNamespace(box=tmp_path / "outbox", env=env, caps=[])

    async def fake_synthesize(text, speech_format, target, *, api_key, model, voice,
                              max_bytes=None, **_where):
        state.caps.append(max_bytes)
        state.box_mode = target.parent.stat().st_mode & 0o777
        target.write_bytes(OGG)
        return len(OGG)

    async def send(message, destination, context):
        pass

    monkeypatch.setattr(notify, "SPACE_PLATFORM_DIR", tmp_path)
    monkeypatch.setattr(notify, "OUTBOX_DIR", state.box)
    monkeypatch.setattr(notify, "WOLTS_DIR", tmp_path)
    monkeypatch.setattr(notify, "dotenv_env", lambda key: env.get(key, ""))
    # Telegram sends through its connector: here it is connected while the
    # lodge has a bot token, so the old "no token" cases are "not connected".
    monkeypatch.setattr(notify, "_connector_up",
                        lambda name: name != "telegram" or bool(env.get("TELEGRAM_BOT_TOKEN")))
    monkeypatch.setattr(notify, "read_session_registry", lambda name: None)
    monkeypatch.setattr(notify, "append_chat_history", Mock())
    monkeypatch.setattr(notify, "SessionRegistry", lambda root: Mock())
    monkeypatch.setattr(notify, "synthesize", fake_synthesize)
    monkeypatch.setitem(notify.SENDERS, "telegram", Plugin(
        "telegram", send, secrets=("TELEGRAM_BOT_TOKEN",),
        max_attachment_bytes=1234, speech_format="opus",
    ))
    return state


@pytest.mark.asyncio
async def test_speak_caps_the_audio_at_what_the_sender_carries(lodge):
    await notify.send_notification("s", "hi\n", None, (), {"text": "hello"})
    assert lodge.caps == [1234]


@pytest.mark.asyncio
async def test_speak_tightens_an_outbox_left_open(lodge):
    lodge.box.mkdir(mode=0o755)
    lodge.box.chmod(0o755)
    await notify.send_notification("s", "hi\n", None, (), {"text": "hello"})
    assert lodge.box_mode == 0o700


@pytest.mark.asyncio
async def test_speech_not_configured_names_no_lodge_path(lodge):
    del lodge.env["OPENAI_API_KEY"]
    with pytest.raises(SpeechError) as error:
        await notify.send_notification("s", "hi\n", None, (), {"text": "hello"})
    assert error.value.reason == "speech_not_configured"
    assert str(notify.WOLTS_DIR) not in str(error.value)
    assert "/" not in str(error.value)


def test_a_lodge_state_file_is_refused_with_its_own_reason(tmp_path):
    state = tmp_path / ".space" / "telegram" / "uploads"
    state.mkdir(parents=True)
    upload = state / "photo.jpg"
    upload.write_bytes(b"jpeg")
    with pytest.raises(outbox.OutboxError) as error:
        outbox.check_source(upload)
    assert ".space folder" in str(error.value)
    assert "copy it into your wolt" in str(error.value)
