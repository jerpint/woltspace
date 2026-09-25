"""Slack final delivery clears one exact native Agent Session status."""

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
        "slack_progress_mode": "agent",
        "slack_progress_ts": "",
        "slack_session_link": "https://lodge.test/tui?session=n00b-session-1",
        **changes,
    }


@pytest.mark.asyncio
async def test_invalid_or_cross_session_link_is_not_attached(monkeypatch):
    routing = route(
        slack_session_link="https://evil.test/tui?session=other-session",
    )
    monkeypatch.setattr(notify, "dotenv_env", lambda key: "xoxb-test")
    send = AsyncMock(return_value={"ok": True})
    status = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(notify, "slack_send", send)
    monkeypatch.setattr(notify, "slack_set_agent_status", status)
    monkeypatch.setattr(notify, "append_chat_history", Mock())
    monkeypatch.setattr(
        notify, "SessionRegistry", lambda root: Mock(update=Mock())
    )

    await notify._send_slack(
        "final", "D123", "1000.1",
        session="n00b-session-1", routing=routing,
    )

    send.assert_awaited_once_with("xoxb-test", "D123", "1000.1", "final")


@pytest.mark.asyncio
async def test_native_agent_progress_posts_final_then_returns_active(monkeypatch):
    routing = route(
        slack_progress_mode="agent",
        slack_progress_ts="",
    )
    monkeypatch.setattr(notify, "dotenv_env", lambda key: "xoxb-test")
    send = AsyncMock(return_value={"ok": True})
    status = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(notify, "slack_send", send)
    monkeypatch.setattr(notify, "slack_set_agent_status", status)
    monkeypatch.setattr(notify, "append_chat_history", Mock())
    update = Mock()
    monkeypatch.setattr(notify, "SessionRegistry", lambda root: Mock(update=update))

    await notify._send_slack(
        "final", "D123", "1000.1",
        session="n00b-session-1", routing=routing,
    )

    send.assert_awaited_once_with(
        "xoxb-test", "D123", "1000.1",
        "final\n\n<https://lodge.test/tui?session=n00b-session-1|Open session>",
    )
    status.assert_awaited_once_with(
        "xoxb-test", "D123", "1000.1", "active"
    )
    update.assert_called_once_with(
        "n00b-session-1", wolt="n00b",
        slack_progress_mode="", slack_progress_ts="",
    )


@pytest.mark.asyncio
async def test_route_mismatch_cannot_claim_progress(monkeypatch):
    monkeypatch.setattr(notify, "read_session_registry", lambda session: route())
    monkeypatch.setattr(notify, "dotenv_env", lambda key: "xoxb-test")
    send = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(notify, "slack_send", send)
    monkeypatch.setattr(notify, "append_chat_history", Mock())

    await notify.send_notification(
        "n00b-session-1", "final",
        explicit={"adapter": "slack", "channel": "DOTHER", "thread_ts": "1000.1"},
    )

    send.assert_awaited_once_with("xoxb-test", "DOTHER", "1000.1", "final")
