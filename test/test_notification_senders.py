"""Built-in dispatch preserves routing, bytes and failure-side state changes."""

import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest

from server import notify
from server.config import DEN_REPLY_FOOTER
from server.notification_types import Plugin


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [
    "explicit_telegram", "session_telegram", "default_first_user",
    "unknown_explicit_adapter_falls_through_to_session_lookup",
    "malformed_explicit_adapter_falls_through", "empty_telegram_falls_through",
    "unknown_session_adapter_falls_through", "no_tunnel", "no_session",
    "explicit_slack_exact", "explicit_slack_mismatch", "slack_thread_mismatch",
    "session_slack", "slack_default_channel", "slack_missing_channel",
    "telegram_missing_token", "slack_missing_token", "no_target",
])
async def test_dispatch_preserves_routes_and_telegram_footer(case, monkeypatch, tmp_path):
    session = "test-session"
    text = "original text\n"
    env = {
        "TELEGRAM_BOT_TOKEN": "telegram-token", "SLACK_BOT_TOKEN": "slack-token",
        "TELEGRAM_ALLOWED_USERS": " 101, 202 ", "SLACK_NOTIFY_CHANNEL": "CDEFAULT",
    }
    routing = {"adapter": "telegram", "chat_id": 303, "wolt": "testwolt"}
    explicit = None
    expected_chat = "303"
    expected_adapter = "telegram"
    expected_error = None
    tunnel = "https://lodge.test"
    if case == "explicit_telegram":
        explicit, expected_chat = {"adapter": "telegram", "chat_id": 404}, "404"
    elif case in ("default_first_user", "no_target"):
        routing, expected_chat = None, "101"
    elif case == "unknown_explicit_adapter_falls_through_to_session_lookup":
        explicit = {"adapter": "unknown", "chat_id": "ignored"}
    elif case == "malformed_explicit_adapter_falls_through":
        explicit = {"adapter": [], "chat_id": "ignored"}
    elif case == "empty_telegram_falls_through":
        explicit = {"adapter": "telegram", "chat_id": ""}
    elif case == "unknown_session_adapter_falls_through":
        routing, expected_chat = {"adapter": "unknown"}, "101"
    elif case == "no_tunnel":
        tunnel = ""
    elif case == "no_session":
        session, expected_chat = "", "101"
    elif "slack" in case:
        expected_adapter, expected_chat = "slack", "C123"
        routing = {
            "adapter": "slack", "chat_id": "C123", "thread_ts": "1.2",
            "wolt": "testwolt", "slack_progress_mode": "agent",
            "slack_progress_ts": "old", "slack_session_link": f"{tunnel}/tui?session={session}",
        }
        if case.startswith("explicit_") or case == "slack_thread_mismatch":
            explicit = {"adapter": "slack", "channel": "C123", "thread_ts": "1.2"}
        if case == "explicit_slack_mismatch":
            explicit["channel"], expected_chat = "COTHER", "COTHER"
        if case == "slack_thread_mismatch":
            explicit["thread_ts"] = "9.9"
        if case in ("slack_default_channel", "slack_missing_channel"):
            routing["chat_id"], expected_chat = "", "CDEFAULT"
    if case.endswith("missing_token"):
        adapter = case.split("_")[0]
        env[f"{adapter.upper()}_BOT_TOKEN"] = ""
        expected_error = f"{adapter.upper()}_BOT_TOKEN not set"
    if case == "slack_missing_channel":
        env["SLACK_NOTIFY_CHANNEL"] = ""
        expected_error = "no slack channel provided and SLACK_NOTIFY_CHANNEL not set"
    if case == "no_target":
        env["TELEGRAM_ALLOWED_USERS"] = ""
        expected_error = "no notification target"

    (tmp_path / "tunnel.json").write_text(json.dumps({"url": tunnel}))
    monkeypatch.setattr(notify, "SPACE_PLATFORM_DIR", tmp_path)
    monkeypatch.setattr(notify, "dotenv_env", lambda key: env.get(key, ""))
    monkeypatch.setattr(notify, "read_session_registry", lambda name: routing)
    telegram, slack, status = AsyncMock(), AsyncMock(), AsyncMock()
    history, update = Mock(), Mock()
    monkeypatch.setattr(notify, "telegram_send", telegram)
    monkeypatch.setattr(notify, "slack_send", slack)
    monkeypatch.setattr(notify, "slack_set_agent_status", status)
    monkeypatch.setattr(notify, "append_chat_history", history)
    monkeypatch.setattr(notify, "SessionRegistry", lambda root: Mock(update=update))

    if expected_error:
        error_type = notify.NoNotificationTarget if case == "no_target" else RuntimeError
        with pytest.raises(error_type, match=expected_error):
            await notify.send_notification(session, text, explicit)
        telegram.assert_not_awaited()
        slack.assert_not_awaited()
        history.assert_not_called()
        return

    result = await notify.send_notification(session, text, explicit)
    key = "chat_id" if expected_adapter == "telegram" else "channel"
    assert result == {"adapter": expected_adapter, key: expected_chat}
    history.assert_called_once_with(expected_adapter, expected_chat, text)
    if expected_adapter == "telegram":
        link = f"{tunnel}/tui?session={session}" if tunnel else f"session={session}"
        footer = f"\n\n---{DEN_REPLY_FOOTER}\n{link}" if session else ""
        telegram.assert_awaited_once_with("telegram-token", expected_chat, text + footer)
        slack.assert_not_awaited()
        status.assert_not_awaited()
        update.assert_not_called()
    else:
        mismatch = case in ("explicit_slack_mismatch", "slack_thread_mismatch")
        suffix = "" if mismatch else f"\n\n<{tunnel}/tui?session={session}|Open session>"
        thread = "9.9" if case == "slack_thread_mismatch" else "1.2"
        slack.assert_awaited_once_with("slack-token", expected_chat, thread, text + suffix)
        telegram.assert_not_awaited()
        if mismatch:
            status.assert_not_awaited()
            update.assert_not_called()
        else:
            status.assert_awaited_once_with("slack-token", expected_chat, thread, "active")
            update.assert_called_once_with(
                session, wolt="testwolt", slack_progress_mode="", slack_progress_ts="",
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["send_fails", "status_fails", "cancelled_during_send", "cancelled_during_status"])
async def test_slack_failure_preserves_cleanup(case, monkeypatch, caplog):
    events = []
    original = RuntimeError("provider error")
    started = asyncio.Event()

    async def transport(*args):
        events.append("send")
        if case == "send_fails":
            raise original
        if case == "cancelled_during_send":
            started.set()
            await asyncio.Future()

    async def set_status(*args):
        assert args == ("token", "C123", "1.2", "active")
        events.append("status")
        if case == "status_fails":
            raise RuntimeError("status error")
        if case == "cancelled_during_status":
            started.set()
            await asyncio.Future()

    update = Mock(side_effect=lambda *a, **kw: events.append("update"))
    history = Mock(side_effect=lambda *a: events.append("history"))
    monkeypatch.setattr(notify, "dotenv_env", lambda key: "token")
    monkeypatch.setattr(notify, "slack_send", transport)
    monkeypatch.setattr(notify, "slack_set_agent_status", set_status)
    monkeypatch.setattr(notify, "append_chat_history", history)
    monkeypatch.setattr(notify, "SessionRegistry", lambda root: Mock(update=update))
    task = asyncio.create_task(notify._send_slack(
        "text", "C123", "1.2", session="test-session",
        routing={"wolt": "testwolt", "slack_progress_mode": "agent", "slack_progress_ts": "old"},
    ))
    if case.startswith("cancelled"):
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif case == "send_fails":
        with pytest.raises(RuntimeError) as error:
            await task
        assert error.value is original
    else:
        assert await task == {"adapter": "slack", "channel": "C123"}
        assert "Could not clear Slack native agent status: status error" in caplog.text
    if case == "cancelled_during_status":
        assert events == ["send", "status"]
        update.assert_not_called()
    else:
        update.assert_called_once_with(
            "test-session", wolt="testwolt", slack_progress_mode="", slack_progress_ts="",
        )
        assert events[:3] == ["send", "status", "update"]
    if case == "status_fails":
        history.assert_called_once_with("slack", "C123", "text")
        assert events[-1] == "history"
    else:
        history.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("eligible", [True, False])
async def test_route_state_is_filtered(eligible, monkeypatch, caplog):
    seen = []

    async def send(message, destination, context):
        seen.append(context)
        assert context.secrets == {"SLACK_BOT_TOKEN": "token"}
        assert context.route_state == ({"slack_progress_mode": "agent"} if eligible else {})
        assert context.updates == {}
        context.updates.update(slack_progress_mode="", adapter="evil", chat_id="stolen", harness_session_id="other")

    monkeypatch.setitem(notify.SENDERS, "slack", Plugin(
        "slack", send, ("SLACK_BOT_TOKEN",), ("slack_progress_mode",),
    ))
    monkeypatch.setattr(notify, "dotenv_env", lambda key: "token")
    monkeypatch.setattr(notify, "append_chat_history", Mock())
    update = Mock()
    monkeypatch.setattr(notify, "SessionRegistry", lambda root: Mock(update=update))
    routing = {"wolt": "testwolt", "slack_progress_mode": "agent", "chat_id": "C123", "harness_session_id": "private"}
    before = dict(routing)
    for _ in range(2):
        await notify._send_slack("text", "C123", session="test-session", routing=routing if eligible else None)
    assert seen[0].updates is not seen[1].updates
    assert routing == before
    if eligible:
        assert update.call_count == 2
        update.assert_called_with("test-session", wolt="testwolt", slack_progress_mode="")
    else:
        update.assert_not_called()
    for key in ("adapter", "chat_id", "harness_session_id"):
        assert f"Ignored undeclared notification state key: {key}" in caplog.text
