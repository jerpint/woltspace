"""Slack callback replaces one exact session-bound progress surface."""

from unittest.mock import AsyncMock, Mock

import pytest

from server import notify


def route(**changes):
    return {
        "name": "n00b-session-1",
        "wolt": "n00b",
        "adapter": "slack",
        "chat_id": "D123",
        "thread_ts": "1000.1",
        "slack_progress_mode": "stream",
        "slack_progress_ts": "2000.1",
        **changes,
    }


class Response:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


@pytest.mark.asyncio
async def test_stream_stop_is_followed_by_exact_final_replacement(monkeypatch):
    post = AsyncMock(side_effect=[Response({"ok": True}), Response({"ok": True})])
    client = AsyncMock()
    client.post = post
    context = AsyncMock()
    context.__aenter__.return_value = client
    monkeypatch.setattr(notify.httpx, "AsyncClient", lambda: context)

    await notify.slack_finish_progress(
        "xoxb-test", "D123", "2000.1", "stream", "🦝 n00b: final"
    )

    assert post.await_args_list[0].args[0].endswith("/chat.stopStream")
    assert "text" not in post.await_args_list[0].kwargs["json"]
    assert post.await_args_list[1].args[0].endswith("/chat.update")
    assert post.await_args_list[1].kwargs["json"]["text"] == "🦝 n00b: final"
    assert "gnawing" not in str(post.await_args_list[1])


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["stream", "message"])
async def test_exact_session_route_replaces_progress_once_and_clears(
    monkeypatch, mode
):
    routing = route(slack_progress_mode=mode)
    monkeypatch.setattr(notify, "read_session_registry", lambda session: routing)
    monkeypatch.setattr(notify, "dotenv_env", lambda key: "xoxb-test")
    finish = AsyncMock(return_value={"ok": True})
    send = AsyncMock(side_effect=AssertionError("must replace, not post"))
    monkeypatch.setattr(notify, "slack_finish_progress", finish)
    monkeypatch.setattr(notify, "slack_send", send)
    monkeypatch.setattr(notify, "append_chat_history", Mock())
    update = Mock()
    monkeypatch.setattr(notify, "SessionRegistry", lambda root: Mock(update=update))

    result = await notify.send_notification(
        "n00b-session-1", "🦝 n00b: final answer",
        explicit={"adapter": "slack", "channel": "D123", "thread_ts": "1000.1"},
    )

    assert result["adapter"] == "slack"
    finish.assert_awaited_once_with(
        "xoxb-test", "D123", "2000.1", mode, "🦝 n00b: final answer"
    )
    update.assert_called_once_with(
        "n00b-session-1", wolt="n00b",
        slack_progress_mode="", slack_progress_ts="",
    )
    assert "final answer" not in str(update.call_args)


@pytest.mark.asyncio
async def test_route_mismatch_cannot_claim_progress(monkeypatch):
    monkeypatch.setattr(notify, "read_session_registry", lambda session: route())
    monkeypatch.setattr(notify, "dotenv_env", lambda key: "xoxb-test")
    send = AsyncMock(return_value={"ok": True})
    finish = AsyncMock(side_effect=AssertionError("mismatched route must not replace"))
    monkeypatch.setattr(notify, "slack_send", send)
    monkeypatch.setattr(notify, "slack_finish_progress", finish)
    monkeypatch.setattr(notify, "append_chat_history", Mock())

    await notify.send_notification(
        "n00b-session-1", "final",
        explicit={"adapter": "slack", "channel": "DOTHER", "thread_ts": "1000.1"},
    )

    send.assert_awaited_once_with("xoxb-test", "DOTHER", "1000.1", "final")


@pytest.mark.asyncio
async def test_failed_replacement_deletes_progress_and_clears_idempotently(monkeypatch):
    routing = route()
    monkeypatch.setattr(notify, "dotenv_env", lambda key: "xoxb-test")
    monkeypatch.setattr(
        notify, "slack_finish_progress", AsyncMock(side_effect=RuntimeError("failed"))
    )
    delete = AsyncMock()
    monkeypatch.setattr(notify, "slack_delete", delete)
    monkeypatch.setattr(notify, "append_chat_history", Mock())
    update = Mock()
    monkeypatch.setattr(notify, "SessionRegistry", lambda root: Mock(update=update))

    with pytest.raises(RuntimeError, match="failed"):
        await notify._send_slack(
            "final", "D123", "1000.1",
            session="n00b-session-1", routing=routing,
        )

    delete.assert_awaited_once_with("xoxb-test", "D123", "2000.1")
    update.assert_called_once_with(
        "n00b-session-1", wolt="n00b",
        slack_progress_mode="", slack_progress_ts="",
    )
