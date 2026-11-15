"""Built-in dispatch preserves routing, bytes and failure-side state changes."""

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
    "telegram_not_connected", "no_target",
])
async def test_dispatch_preserves_routes_and_telegram_footer(case, monkeypatch, tmp_path):
    session = "test-session"
    text = "original text\n"
    env = {
        "TELEGRAM_BOT_TOKEN": "telegram-token", "TELEGRAM_ALLOWED_USERS": " 101, 202 ",
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
    if case == "telegram_not_connected":
        expected_error = "telegram is not connected to the lodge"
    if case == "no_target":
        env["TELEGRAM_ALLOWED_USERS"] = ""
        expected_error = "no notification target"

    (tmp_path / "tunnel.json").write_text(json.dumps({"url": tunnel}))
    monkeypatch.setattr(notify, "SPACE_PLATFORM_DIR", tmp_path)
    monkeypatch.setattr(notify, "dotenv_env", lambda key: env.get(key, ""))
    monkeypatch.setattr(notify, "read_session_registry", lambda name: routing)
    monkeypatch.setattr(notify, "_connector_up", lambda name: case != "telegram_not_connected")
    telegram = AsyncMock()
    history, update = Mock(), Mock()
    monkeypatch.setattr(notify, "telegram_send", telegram)
    monkeypatch.setattr(notify, "append_chat_history", history)
    monkeypatch.setattr(notify, "SessionRegistry", lambda root: Mock(update=update))

    if expected_error:
        error_type = notify.NoNotificationTarget if case == "no_target" else RuntimeError
        with pytest.raises(error_type, match=expected_error):
            await notify.send_notification(session, text, explicit)
        telegram.assert_not_awaited()
        history.assert_not_called()
        return

    result = await notify.send_notification(session, text, explicit)
    assert result == {"adapter": expected_adapter, "chat_id": expected_chat}
    history.assert_called_once_with(expected_adapter, expected_chat, text)
    link = f"{tunnel}/tui?session={session}" if tunnel else f"session={session}"
    footer = f"\n\n---{DEN_REPLY_FOOTER}\n{link}" if session else ""
    # The token stays with the connector: the transport is handed none.
    telegram.assert_awaited_once_with("", expected_chat, text + footer)
    update.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("eligible", [True, False])
async def test_route_state_is_filtered(eligible, monkeypatch, caplog):
    seen = []

    async def send(message, destination, context):
        seen.append(context)
        assert context.secrets == {"TELEGRAM_BOT_TOKEN": "token"}
        assert context.route_state == ({"progress_mode": "agent"} if eligible else {})
        assert context.updates == {}
        context.updates.update(progress_mode="", adapter="evil", chat_id="stolen", harness_session_id="other")

    monkeypatch.setitem(notify.SENDERS, "telegram", Plugin(
        "telegram", send, ("TELEGRAM_BOT_TOKEN",), ("progress_mode",),
    ))
    monkeypatch.setattr(notify, "_connector_up", lambda name: True)
    monkeypatch.setattr(notify, "dotenv_env", lambda key: "token")
    monkeypatch.setattr(notify, "append_chat_history", Mock())
    update = Mock()
    monkeypatch.setattr(notify, "SessionRegistry", lambda root: Mock(update=update))
    routing = {"wolt": "testwolt", "progress_mode": "agent", "chat_id": "123", "harness_session_id": "private"}
    before = dict(routing)
    for _ in range(2):
        await notify._send("telegram", "test-session", "text", "123", routing=routing if eligible else None)
    assert seen[0].updates is not seen[1].updates
    assert routing == before
    if eligible:
        assert update.call_count == 2
        update.assert_called_with("test-session", wolt="testwolt", progress_mode="")
    else:
        update.assert_not_called()
    for key in ("adapter", "chat_id", "harness_session_id"):
        assert f"Ignored undeclared notification state key: {key}" in caplog.text
