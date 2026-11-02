"""POST /notify hands attachments to core and answers a refusal with its reason."""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container" / "lib"))

import server.app as server_app
from server import notify
from server.notify import AttachmentError, NoNotificationTarget

ENTRY = {"id": "a" * 32, "name": "a.html"}
SENT = {"adapter": "telegram", "chat_id": "101"}


def _client() -> TestClient:
    return TestClient(server_app.app, base_url="http://localhost:7777")


def _attachments(send: AsyncMock):
    """What the route passed as attachments: by keyword, or fourth positional."""
    call = send.await_args
    return call.kwargs["attachments"] if "attachments" in call.kwargs else call.args[3]


def test_attachments_are_passed_through(monkeypatch):
    send = AsyncMock(return_value=dict(SENT))
    monkeypatch.setattr(server_app, "send_notification", send)
    monkeypatch.setattr(server_app, "bot_log", Mock())

    response = _client().post(
        "/notify", json={"session": "s", "message": "hi", "attachments": [ENTRY]},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, **SENT}
    send.assert_awaited_once()
    assert send.await_args.args[:2] == ("s", "hi")
    assert _attachments(send) == [ENTRY]


def test_explicit_route_and_attachments_travel_together(monkeypatch):
    send = AsyncMock(return_value=dict(SENT))
    monkeypatch.setattr(server_app, "send_notification", send)
    monkeypatch.setattr(server_app, "bot_log", Mock())

    response = _client().post("/notify", json={
        "session": "s", "message": "hi", "adapter": "telegram", "chat_id": "42",
        "attachments": [ENTRY],
    })

    assert response.status_code == 200
    call = send.await_args
    explicit = call.kwargs["explicit"] if "explicit" in call.kwargs else call.args[2]
    assert explicit == {"adapter": "telegram", "chat_id": "42"}
    assert _attachments(send) == [ENTRY]


@pytest.mark.parametrize("body", [
    {"message": "hi"},
    {"message": "hi", "attachments": None},
    {"message": "hi", "attachments": []},
])
def test_request_without_attachments_passes_an_empty_list(monkeypatch, body):
    send = AsyncMock(return_value=dict(SENT))
    monkeypatch.setattr(server_app, "send_notification", send)
    monkeypatch.setattr(server_app, "bot_log", Mock())

    response = _client().post("/notify", json=body)

    assert response.status_code == 200
    assert response.json() == {"ok": True, **SENT}
    send.assert_awaited_once()
    assert _attachments(send) == []


@pytest.mark.parametrize("reason, status", [
    ("attachment_invalid", 400),
    ("attachment_not_found", 400),
    ("attachments_unsupported", 400),
    ("attachment_too_large", 413),
])
def test_attachment_errors_map_to_400_or_413_with_a_reason(monkeypatch, capsys, reason, status):
    send = AsyncMock(side_effect=AttachmentError(reason, "the file cannot go"))
    log = Mock()
    monkeypatch.setattr(server_app, "send_notification", send)
    monkeypatch.setattr(server_app, "bot_log", log)

    response = _client().post("/notify", json={"message": "hi", "attachments": [ENTRY]})

    assert response.status_code == status
    assert response.json() == {"ok": False, "error": "the file cannot go", "reason": reason}
    assert "[notify] attachment refused: the file cannot go" in capsys.readouterr().out
    log.assert_not_called()


def test_no_target_is_still_409(monkeypatch):
    send = AsyncMock(side_effect=NoNotificationTarget("no notification target — set TELEGRAM_BOT_TOKEN"))
    monkeypatch.setattr(server_app, "send_notification", send)
    monkeypatch.setattr(server_app, "bot_log", Mock())

    response = _client().post("/notify", json={"message": "hi", "attachments": [ENTRY]})

    assert response.status_code == 409
    assert response.json()["reason"] == "no_notification_target"
    assert _attachments(send) == [ENTRY]


def test_sent_log_carries_attachment_metadata_only(monkeypatch):
    metadata = [{"name": "a.html", "content_type": "text/html", "size": 11}]
    send = AsyncMock(return_value={**SENT, "attachments": metadata})
    log = Mock()
    monkeypatch.setattr(server_app, "send_notification", send)
    monkeypatch.setattr(server_app, "bot_log", log)

    response = _client().post(
        "/notify", json={"session": "s", "message": "hi", "attachments": [ENTRY]},
    )

    assert response.json() == {"ok": True, **SENT, "attachments": metadata}
    # The whole event: a name, a type and a size. No outbox id, no path, no content.
    log.assert_called_once_with("notify_sent", {
        "session": "s", **SENT, "attachments": metadata, "message": "hi",
    })


def test_a_blank_error_names_its_class(monkeypatch, capsys):
    # httpx timeouts usually carry no text, and an upload is where one is likeliest.
    send = AsyncMock(side_effect=httpx.WriteTimeout(""))
    monkeypatch.setattr(server_app, "send_notification", send)
    monkeypatch.setattr(server_app, "bot_log", Mock())

    response = _client().post("/notify", json={"message": "hi", "attachments": [ENTRY]})

    assert response.status_code == 500
    assert response.json() == {"error": "WriteTimeout"}
    assert "[notify] error: WriteTimeout" in capsys.readouterr().out


def test_a_worded_error_is_unchanged(monkeypatch, capsys):
    send = AsyncMock(side_effect=RuntimeError("telegram API is on fire"))
    monkeypatch.setattr(server_app, "send_notification", send)
    monkeypatch.setattr(server_app, "bot_log", Mock())

    response = _client().post("/notify", json={"message": "hi"})

    assert response.status_code == 500
    assert response.json() == {"error": "telegram API is on fire"}
    assert "[notify] error: telegram API is on fire" in capsys.readouterr().out


@pytest.mark.parametrize("attachments", ["not a list", {"id": "a" * 32}, [ENTRY, ENTRY]])
def test_a_malformed_request_is_refused_by_the_real_core(monkeypatch, tmp_path, attachments):
    # The real send_notification runs here. Its outbound seam is replaced and
    # must stay unreached: core refuses before it picks a destination.
    telegram_send, telegram_send_document = AsyncMock(), AsyncMock()
    monkeypatch.setattr(notify, "telegram_send", telegram_send)
    monkeypatch.setattr(notify, "telegram_send_document", telegram_send_document)
    monkeypatch.setattr(notify, "OUTBOX_DIR", tmp_path / "outbox")
    monkeypatch.setattr(server_app, "bot_log", Mock())

    response = _client().post("/notify", json={"message": "hi", "attachments": attachments})

    assert response.status_code == 400
    assert response.json() == {
        "ok": False,
        "error": "attachments must be a list with at most one {id, name} entry",
        "reason": "attachment_invalid",
    }
    telegram_send.assert_not_awaited()
    telegram_send_document.assert_not_awaited()
