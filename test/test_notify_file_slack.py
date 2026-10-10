"""Slack file sends: the sender's choice of delivery, and the three-call external upload."""

import asyncio
import json
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

import outbox
from server import notification_senders as senders
from server import notify
from server.notification_senders import (
    SLACK_MAX_ATTACHMENT_BYTES, SLACK_MISSING_FILES_SCOPE, UPLOAD_TIMEOUT,
    slack_sender, slack_upload_file,
)
from server.notification_types import Attachment, OutboundMessage, SendContext

LINK = "https://lodge.test/tui?session=s"
COMMENT = f"hi\n\n<{LINK}|Open session>"
DESTINATION = {"channel": "C0123ABCD", "thread_ts": "1.2"}
CLEARED = {"slack_progress_mode": "", "slack_progress_ts": ""}

GET_URL = "https://slack.com/api/files.getUploadURLExternal"
UPLOAD_URL = "https://files.slack.test/upload/v1/abc"
COMPLETE_URL = "https://slack.com/api/files.completeUploadExternal"
MISSING_SCOPE = {"ok": False, "error": "missing_scope", "needed": "files:write", "provided": "chat:write"}
METHOD = {"get_url": "files.getUploadURLExternal", "complete": "files.completeUploadExternal"}
NOT_A_FILE_DESTINATION = (
    "Slack can attach a file only to a channel or DM id (C..., G..., D...), "
    "not to a member id; send the file from the Slack thread, or send text only"
)
# What real Slack answered when channel_id was a member id.
BAD_CHANNEL = "[ERROR] input must match regex pattern: ^[CGDZ][A-Z0-9]{8,}$ [json-pointer:/channel_id]"


@pytest.fixture
def attachment(tmp_path):
    path = tmp_path / ("a" * 32)
    path.write_bytes(b"<h1>hi</h1>")
    return Attachment(path, "weekly-update.html", 11, "text/html")


def _sender():
    transport, set_status, upload = AsyncMock(), AsyncMock(), AsyncMock()
    return slack_sender(transport, set_status, upload), transport, set_status, upload


def _context(mode=""):
    route_state = {"slack_progress_mode": mode, "slack_progress_ts": "old"} if mode else {}
    return SendContext({"SLACK_BOT_TOKEN": "tok"}, route_state)


@pytest.mark.asyncio
async def test_file_goes_to_the_thread_with_the_text_and_session_link_as_comment(attachment):
    send, transport, _, upload = _sender()
    await send(OutboundMessage("hi", LINK, (attachment,)), DESTINATION, _context())
    upload.assert_awaited_once_with(
        "tok", "C0123ABCD", "1.2", attachment, "hi\n\n<https://lodge.test/tui?session=s|Open session>",
    )
    transport.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_status_is_cleared_after_a_file_send(attachment):
    send, transport, set_status, upload = _sender()
    context = _context("agent")
    await send(OutboundMessage("hi", None, (attachment,)), DESTINATION, context)
    upload.assert_awaited_once_with("tok", "C0123ABCD", "1.2", attachment, "hi")
    transport.assert_not_awaited()
    set_status.assert_awaited_once_with("tok", "C0123ABCD", "1.2", "active")
    assert context.updates == CLEARED


@pytest.mark.asyncio
async def test_agent_status_is_cleared_when_the_upload_fails(attachment):
    send, _, set_status, upload = _sender()
    original = RuntimeError("file_uploads_disabled")
    upload.side_effect = original
    context = _context("agent")
    with pytest.raises(RuntimeError) as error:
        await send(OutboundMessage("hi", None, (attachment,)), DESTINATION, context)
    assert error.value is original
    set_status.assert_awaited_once_with("tok", "C0123ABCD", "1.2", "active")
    assert context.updates == CLEARED


@pytest.mark.asyncio
async def test_agent_status_is_cleared_when_the_upload_is_cancelled(attachment):
    started = asyncio.Event()

    async def upload(*args):
        started.set()
        await asyncio.Future()

    set_status = AsyncMock()
    send = slack_sender(AsyncMock(), set_status, upload)
    context = _context("agent")
    task = asyncio.create_task(send(OutboundMessage("hi", None, (attachment,)), DESTINATION, context))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    set_status.assert_awaited_once_with("tok", "C0123ABCD", "1.2", "active")
    assert context.updates == CLEARED


@pytest.mark.asyncio
@pytest.mark.parametrize("mode, destination", [
    ("", DESTINATION),
    ("agent", {"channel": "C0123ABCD"}),
])
async def test_no_agent_mode_means_no_status_call(mode, destination, attachment):
    send, _, set_status, upload = _sender()
    context = _context(mode)
    await send(OutboundMessage("hi", None, (attachment,)), destination, context)
    upload.assert_awaited_once_with("tok", "C0123ABCD", destination.get("thread_ts"), attachment, "hi")
    set_status.assert_not_awaited()
    assert context.updates == {}


@pytest.mark.asyncio
async def test_text_only_message_never_touches_the_upload_transport():
    send, transport, _, upload = _sender()
    await send(OutboundMessage("hi", LINK), DESTINATION, _context())
    transport.assert_awaited_once_with("tok", "C0123ABCD", "1.2", COMMENT)
    upload.assert_not_awaited()


@pytest.mark.asyncio
async def test_file_without_an_upload_transport_is_refused_and_no_text_is_sent(attachment):
    transport, set_status = AsyncMock(), AsyncMock()
    send = slack_sender(transport, set_status)
    with pytest.raises(RuntimeError, match="^slack sender has no upload transport$"):
        await send(OutboundMessage("hi", LINK, (attachment,)), DESTINATION, _context())
    transport.assert_not_awaited()


@pytest.mark.asyncio
async def test_more_than_one_file_is_refused_and_nothing_is_sent(attachment):
    send, transport, _, upload = _sender()
    with pytest.raises(RuntimeError, match="^slack sender carries one file per message$"):
        await send(OutboundMessage("hi", LINK, (attachment, attachment)), DESTINATION, _context())
    upload.assert_not_awaited()
    transport.assert_not_awaited()


class _Slack:
    """Slack's three upload endpoints behind a real httpx.AsyncClient on a MockTransport.

    httpx's own request encoding runs, so a body an async client cannot send
    fails here the way it would against Slack, and nothing reaches the network.
    """

    def __init__(self, monkeypatch, **answers):
        self.requests, self.post_kwargs = [], []
        responses = {
            GET_URL: answers.get("get_url", httpx.Response(
                200, json={"ok": True, "upload_url": UPLOAD_URL, "file_id": "F123"})),
            UPLOAD_URL: answers.get("upload", httpx.Response(200, text="OK - 11")),
            COMPLETE_URL: answers.get("complete", httpx.Response(
                200, json={"ok": True, "files": [{"id": "F123"}]})),
        }
        slack = self

        def handler(request):
            slack.requests.append(request)
            return responses[str(request.url)]

        class Client(httpx.AsyncClient):
            def __init__(self, **kwargs):
                super().__init__(transport=httpx.MockTransport(handler), **kwargs)

            async def post(self, url, **kwargs):
                slack.post_kwargs.append(kwargs)
                return await super().post(url, **kwargs)

        monkeypatch.setattr(senders.httpx, "AsyncClient", Client)

    @property
    def urls(self):
        return [str(request.url) for request in self.requests]


@pytest.mark.asyncio
async def test_upload_makes_the_three_calls_in_order(attachment, monkeypatch):
    slack = _Slack(monkeypatch)
    result = await slack_upload_file("tok", "C0123ABCD", "1.2", attachment, COMMENT)
    assert slack.urls == [GET_URL, UPLOAD_URL, COMPLETE_URL]
    get_url, upload, complete = slack.requests
    assert get_url.headers["Authorization"] == "Bearer tok"
    assert get_url.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert urllib.parse.parse_qs(get_url.content.decode()) == {
        "filename": ["weekly-update.html"], "length": ["11"],
    }
    assert upload.content == b"<h1>hi</h1>"
    assert "Authorization" not in upload.headers
    assert upload.headers["Content-Length"] == "11"
    assert "Transfer-Encoding" not in upload.headers
    assert complete.headers["Authorization"] == "Bearer tok"
    assert json.loads(complete.content) == {
        "files": [{"id": "F123", "title": "weekly-update.html"}],
        "channel_id": "C0123ABCD",
        "thread_ts": "1.2",
        "initial_comment": COMMENT,
    }
    assert len(slack.post_kwargs) == 3
    assert all(kwargs["timeout"] is UPLOAD_TIMEOUT for kwargs in slack.post_kwargs)
    assert result == {"ok": True, "files": [{"id": "F123"}]}


@pytest.mark.asyncio
async def test_upload_carries_a_file_larger_than_one_read(tmp_path, monkeypatch):
    body = bytes(range(256)) * 1024 + b"tail"
    path = tmp_path / ("b" * 32)
    path.write_bytes(body)
    slack = _Slack(monkeypatch)
    await slack_upload_file("tok", "C0123ABCD", None, Attachment(path, "big.bin", len(body), "application/octet-stream"), "")
    upload = slack.requests[1]
    assert upload.content == body
    assert upload.headers["Content-Length"] == str(len(body))


@pytest.mark.asyncio
@pytest.mark.parametrize("thread_ts", [None, ""])
async def test_upload_omits_thread_and_comment_when_absent(thread_ts, attachment, monkeypatch):
    slack = _Slack(monkeypatch)
    await slack_upload_file("tok", "C0123ABCD", thread_ts, attachment, "")
    assert json.loads(slack.requests[2].content) == {
        "files": [{"id": "F123", "title": "weekly-update.html"}],
        "channel_id": "C0123ABCD",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("failing, calls_made", [("get_url", 1), ("complete", 3)])
async def test_missing_scope_names_the_scope_and_the_reinstall(failing, calls_made, attachment, monkeypatch):
    slack = _Slack(monkeypatch, **{failing: httpx.Response(200, json=MISSING_SCOPE)})
    with pytest.raises(RuntimeError) as error:
        await slack_upload_file("tok", "C0123ABCD", "1.2", attachment, "hi")
    assert str(error.value) == SLACK_MISSING_FILES_SCOPE
    assert "files:write" in SLACK_MISSING_FILES_SCOPE and "reinstall" in SLACK_MISSING_FILES_SCOPE
    assert len(slack.requests) == calls_made


@pytest.mark.asyncio
@pytest.mark.parametrize("failing", ["get_url", "complete"])
@pytest.mark.parametrize("answer, message", [
    ({"ok": False, "error": "missing_scope"}, SLACK_MISSING_FILES_SCOPE),
    ({"ok": False, "error": "missing_scope", "needed": "chat:write"},
     "Slack app lacks the chat:write scope: add it to the app manifest and reinstall the app."),
], ids=["no-scope-named", "another-scope-named"])
async def test_missing_scope_blames_the_scope_slack_names(failing, answer, message, attachment, monkeypatch):
    _Slack(monkeypatch, **{failing: httpx.Response(200, json=answer)})
    with pytest.raises(RuntimeError) as error:
        await slack_upload_file("tok", "C0123ABCD", "1.2", attachment, "hi")
    assert str(error.value) == message


@pytest.mark.asyncio
@pytest.mark.parametrize("ticket", [
    {"ok": True}, {"ok": True, "upload_url": UPLOAD_URL}, {"ok": True, "file_id": "F123"},
], ids=["neither", "no-file-id", "no-upload-url"])
async def test_an_upload_ticket_without_its_url_or_file_id_stops_before_the_bytes(ticket, attachment, monkeypatch):
    slack = _Slack(monkeypatch, get_url=httpx.Response(200, json=ticket))
    with pytest.raises(RuntimeError) as error:
        await slack_upload_file("tok", "C0123ABCD", "1.2", attachment, "hi")
    assert str(error.value) == "slack gave no upload URL"
    assert slack.urls == [GET_URL]


@pytest.mark.asyncio
@pytest.mark.parametrize("failing", ["get_url", "complete"])
@pytest.mark.parametrize("answer, error", [
    ({"ok": False, "error": "file_uploads_disabled"}, "file_uploads_disabled"),
    ({"ok": False}, "unknown error"),
    ({"ok": False, "error": "invalid_arguments", "response_metadata": {"messages": [BAD_CHANNEL]}},
     f"invalid_arguments ({BAD_CHANNEL})"),
    ({"ok": False, "error": "invalid_arguments", "response_metadata": {"messages": ["first", "second"]}},
     "invalid_arguments (first; second)"),
    ({"ok": False, "error": "invalid_arguments", "response_metadata": {"messages": []}}, "invalid_arguments"),
    ({"ok": False, "error": "invalid_arguments", "response_metadata": {"messages": "not a list"}},
     "invalid_arguments"),
    ({"ok": False, "error": "invalid_arguments", "response_metadata": {"messages": [7, None]}},
     "invalid_arguments"),
    ({"ok": False, "error": "invalid_arguments", "response_metadata": "not an object"}, "invalid_arguments"),
], ids=[
    "error-code", "no-error-code", "slacks-explanation", "two-explanations", "empty-explanations",
    "explanations-not-a-list", "explanations-not-text", "metadata-not-an-object",
])
async def test_a_slack_error_names_the_call_and_carries_slacks_explanation(failing, answer, error, attachment, monkeypatch):
    _Slack(monkeypatch, **{failing: httpx.Response(200, json=answer)})
    with pytest.raises(RuntimeError) as raised:
        await slack_upload_file("tok", "C0123ABCD", "1.2", attachment, "hi")
    assert str(raised.value) == f"slack {METHOD[failing]}: {error}"


@pytest.mark.asyncio
@pytest.mark.parametrize("failing", ["get_url", "complete"])
@pytest.mark.parametrize("answer, status", [
    (httpx.Response(502, text="<html>Bad Gateway</html>"), 502),
    (httpx.Response(200, text=""), 200),
    (httpx.Response(200, json=["not", "an", "object"]), 200),
], ids=["html-error-page", "empty-body", "json-but-not-an-object"])
async def test_a_reply_that_is_not_json_names_the_call_and_the_http_status(failing, answer, status, attachment, monkeypatch):
    _Slack(monkeypatch, **{failing: answer})
    with pytest.raises(RuntimeError) as raised:
        await slack_upload_file("tok", "C0123ABCD", "1.2", attachment, "hi")
    assert str(raised.value) == f"slack {METHOD[failing]} answered HTTP {status}"


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["U0123ABCD", "W0123ABCD", "C123", "c0123abcd", "C0123ABCD\n", ""])
async def test_a_destination_that_cannot_take_a_file_is_refused_before_any_call(channel, attachment, monkeypatch):
    slack = _Slack(monkeypatch)
    with pytest.raises(RuntimeError) as raised:
        await slack_upload_file("tok", channel, None, attachment, "hi")
    assert str(raised.value) == NOT_A_FILE_DESTINATION
    assert slack.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["C0123ABCD", "G0123ABCD", "D0123ABCD", "Z0123ABCD", "D0123ABCDEFGH"])
async def test_channel_group_and_dm_ids_take_a_file(channel, attachment, monkeypatch):
    slack = _Slack(monkeypatch)
    await slack_upload_file("tok", channel, None, attachment, "")
    assert json.loads(slack.requests[2].content)["channel_id"] == channel


@pytest.mark.asyncio
async def test_failed_byte_upload_raises_with_the_status(attachment, monkeypatch):
    slack = _Slack(monkeypatch, upload=httpx.Response(500, text="boom"))
    with pytest.raises(RuntimeError) as error:
        await slack_upload_file("tok", "C0123ABCD", "1.2", attachment, "hi")
    assert str(error.value) == "slack upload failed: HTTP 500"
    assert slack.urls == [GET_URL, UPLOAD_URL]


def test_registered_slack_sender_declares_fifty_megabytes():
    assert SLACK_MAX_ATTACHMENT_BYTES == 50 * 1024 * 1024
    assert notify.SENDERS["slack"].max_attachment_bytes == 50 * 1024 * 1024


@pytest.mark.asyncio
async def test_registered_slack_sender_uploads_through_the_notify_patch_point(attachment, monkeypatch):
    upload, text = AsyncMock(), AsyncMock()
    monkeypatch.setattr(notify, "slack_upload_file", upload)
    monkeypatch.setattr(notify, "slack_send", text)
    await notify.SENDERS["slack"].send(
        OutboundMessage("hi", LINK, (attachment,)), DESTINATION, _context(),
    )
    upload.assert_awaited_once_with("tok", "C0123ABCD", "1.2", attachment, COMMENT)
    text.assert_not_awaited()


@pytest.fixture
def lodge(monkeypatch, tmp_path):
    """What core needs around it for a Slack send that starts at send_notification."""
    session = "test-session"
    link = f"https://lodge.test/tui?session={session}"
    source = tmp_path / "weekly-update.html"
    source.write_bytes(b"<h1>hi</h1>")
    box = tmp_path / "outbox"
    entry = outbox.stage(source, box)
    state = SimpleNamespace(
        session=session, comment=f"hi\n\n\n<{link}|Open session>", entry=box / entry,
        request=[{"id": entry, "name": "weekly-update.html"}],
        routing={
            "adapter": "slack", "chat_id": "C0123ABCD", "thread_ts": "1.2", "wolt": "testwolt",
            "slack_progress_mode": "agent", "slack_progress_ts": "old",
            "slack_session_link": link,
        },
        text=AsyncMock(), status=AsyncMock(), history=Mock(), update=Mock(),
    )
    monkeypatch.setattr(notify, "OUTBOX_DIR", box)
    monkeypatch.setattr(notify, "dotenv_env", lambda key: {"SLACK_BOT_TOKEN": "tok"}.get(key, ""))
    monkeypatch.setattr(notify, "read_session_registry", lambda name: state.routing)
    monkeypatch.setattr(notify, "slack_send", state.text)
    monkeypatch.setattr(notify, "slack_set_agent_status", state.status)
    monkeypatch.setattr(notify, "append_chat_history", state.history)
    monkeypatch.setattr(notify, "SessionRegistry", lambda root: Mock(update=state.update))
    return state


@pytest.mark.asyncio
async def test_end_to_end_through_send_notification(lodge, monkeypatch):
    seen = []

    async def upload(token, channel, thread_ts, attachment, comment):
        # Read here: core removes the outbox entry once the send returns.
        seen.append((token, channel, thread_ts, attachment, comment, attachment.path.read_bytes()))

    monkeypatch.setattr(notify, "slack_upload_file", upload)
    result = await notify.send_notification(
        lodge.session, "hi\n", {"adapter": "slack", "channel": "C0123ABCD", "thread_ts": "1.2"},
        lodge.request,
    )

    assert seen == [(
        "tok", "C0123ABCD", "1.2",
        Attachment(lodge.entry, "weekly-update.html", 11, "text/html"),
        lodge.comment, b"<h1>hi</h1>",
    )]
    lodge.text.assert_not_awaited()
    lodge.status.assert_awaited_once_with("tok", "C0123ABCD", "1.2", "active")
    lodge.update.assert_called_once_with(
        lodge.session, wolt="testwolt", slack_progress_mode="", slack_progress_ts="",
    )
    lodge.history.assert_called_once_with(
        "slack", "C0123ABCD", "hi\n\n[file sent: weekly-update.html, text/html, 11 bytes]",
    )
    assert result == {
        "adapter": "slack", "channel": "C0123ABCD",
        "attachments": [{"name": "weekly-update.html", "content_type": "text/html", "size": 11}],
    }
    assert not lodge.entry.exists()


@pytest.mark.asyncio
async def test_end_to_end_a_failed_upload_clears_the_status_and_the_outbox(lodge, monkeypatch):
    original = RuntimeError(SLACK_MISSING_FILES_SCOPE)
    monkeypatch.setattr(notify, "slack_upload_file", AsyncMock(side_effect=original))

    with pytest.raises(RuntimeError) as error:
        await notify.send_notification(lodge.session, "hi\n", None, lodge.request)

    assert error.value is original
    lodge.text.assert_not_awaited()
    lodge.status.assert_awaited_once_with("tok", "C0123ABCD", "1.2", "active")
    lodge.update.assert_called_once_with(
        lodge.session, wolt="testwolt", slack_progress_mode="", slack_progress_ts="",
    )
    lodge.history.assert_not_called()
    assert not lodge.entry.exists()


@pytest.mark.asyncio
async def test_end_to_end_the_staged_bytes_are_on_the_wire_before_the_entry_is_removed(lodge, monkeypatch):
    slack = _Slack(monkeypatch)

    await notify.send_notification(lodge.session, "hi\n", None, lodge.request)

    assert slack.urls == [GET_URL, UPLOAD_URL, COMPLETE_URL]
    assert slack.requests[1].content == b"<h1>hi</h1>"
    assert json.loads(slack.requests[2].content) == {
        "files": [{"id": "F123", "title": "weekly-update.html"}],
        "channel_id": "C0123ABCD",
        "thread_ts": "1.2",
        "initial_comment": lodge.comment,
    }
    assert not lodge.entry.exists()


@pytest.mark.asyncio
async def test_end_to_end_a_member_id_as_the_default_channel_is_refused_before_any_call(lodge, monkeypatch):
    env = {"SLACK_BOT_TOKEN": "tok", "SLACK_NOTIFY_CHANNEL": "U0123ABCD"}
    monkeypatch.setattr(notify, "dotenv_env", lambda key: env.get(key, ""))
    lodge.routing.update(chat_id="", thread_ts=None)
    slack = _Slack(monkeypatch)

    with pytest.raises(RuntimeError) as raised:
        await notify.send_notification(lodge.session, "hi\n", None, lodge.request)

    assert str(raised.value) == NOT_A_FILE_DESTINATION
    assert slack.requests == []
    lodge.text.assert_not_awaited()
    lodge.history.assert_not_called()
    assert not lodge.entry.exists()


def test_both_slack_app_manifests_request_files_write_and_agree():
    root = Path(__file__).resolve().parent.parent
    skill = json.loads((root / "container/skills/setup-slack/references/manifest.json").read_text())
    doc = (root / "docs/slack-dm-agent-mvp.md").read_text()
    documented = json.loads(doc.split("```json\n", 1)[1].split("```", 1)[0])
    assert skill["oauth_config"]["scopes"]["bot"] == [
        "assistant:write", "chat:write", "files:write", "im:history",
    ]
    assert documented == skill
