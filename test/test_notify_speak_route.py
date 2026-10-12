"""POST /notify hands `speak` to core and answers a speech failure with its reason."""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container" / "lib"))

import server.app as server_app
from server import notify
from server.notify import AttachmentError
from server.speech import SpeechError

SENT = {"adapter": "telegram", "chat_id": "101"}


def _client() -> TestClient:
    return TestClient(server_app.app, base_url="http://localhost:7777")


def test_speak_is_passed_through_to_core(monkeypatch):
    send = AsyncMock(return_value=dict(SENT))
    monkeypatch.setattr(server_app, "send_notification", send)
    monkeypatch.setattr(server_app, "bot_log", Mock())

    response = _client().post(
        "/notify", json={"session": "s", "message": "🦝 Bob: hi", "speak": {"text": "hi"}},
    )

    assert response.status_code == 200
    assert send.await_args.args[:2] == ("s", "🦝 Bob: hi")
    assert send.await_args.kwargs["speak"] == {"text": "hi"}


def test_a_request_without_speak_makes_the_call_it_always_made(monkeypatch):
    send = AsyncMock(return_value=dict(SENT))
    monkeypatch.setattr(server_app, "send_notification", send)
    monkeypatch.setattr(server_app, "bot_log", Mock())

    _client().post("/notify", json={"session": "s", "message": "hi"})

    assert "speak" not in send.await_args.kwargs


@pytest.mark.parametrize("reason, status", [
    ("speech_failed", 502), ("speech_not_configured", 400),
])
def test_speech_errors_answer_with_their_reason(monkeypatch, capsys, reason, status):
    send = AsyncMock(side_effect=SpeechError(reason, "the speech service answered HTTP 429"))
    log = Mock()
    monkeypatch.setattr(server_app, "send_notification", send)
    monkeypatch.setattr(server_app, "bot_log", log)

    response = _client().post(
        "/notify", json={"session": "s", "message": "hi", "speak": {"text": "hi"}},
    )

    assert response.status_code == status
    assert response.json() == {
        "ok": False, "error": "the speech service answered HTTP 429", "reason": reason,
    }
    log.assert_not_called()
    assert "[notify] speech failed: the speech service answered HTTP 429" in capsys.readouterr().out


@pytest.mark.parametrize("reason", ["speak_invalid", "speak_too_long", "speech_unsupported"])
def test_speak_refusals_are_400_with_a_reason(monkeypatch, capsys, reason):
    send = AsyncMock(side_effect=AttachmentError(reason, "refused"))
    monkeypatch.setattr(server_app, "send_notification", send)

    response = _client().post(
        "/notify", json={"session": "s", "message": "hi", "speak": {"text": "hi"}},
    )

    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": "refused", "reason": reason}


def test_a_malformed_speak_is_refused_by_the_real_core(monkeypatch, tmp_path):
    # The real send_notification runs here. Its outbound seam and the speech
    # client are replaced and must stay unreached: core refuses before it
    # picks a destination.
    telegram_send, telegram_send_voice, synthesize = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(notify, "telegram_send", telegram_send)
    monkeypatch.setattr(notify, "telegram_send_voice", telegram_send_voice)
    monkeypatch.setattr(notify, "synthesize", synthesize)
    monkeypatch.setattr(notify, "OUTBOX_DIR", tmp_path / "outbox")
    monkeypatch.setattr(server_app, "bot_log", Mock())

    response = _client().post(
        "/notify", json={"session": "s", "message": "hi", "speak": "say this"},
    )

    assert response.status_code == 400
    assert response.json() == {
        "ok": False, "error": "speak must be {text} with the words to say",
        "reason": "speak_invalid",
    }
    telegram_send.assert_not_awaited()
    telegram_send_voice.assert_not_awaited()
    synthesize.assert_not_awaited()
