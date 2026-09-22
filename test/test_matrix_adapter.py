import sys
from pathlib import Path

import pytest


pytest.importorskip("nio")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "container"))
sys.path.insert(0, str(ROOT / "container" / "lib"))

import bot.matrix_adapter as adapter_module  # noqa: E402
from bot.matrix_adapter import MatrixAdapter, allowed_users, prompt_for, trusted_devices  # noqa: E402


def test_inbound_prompt_keeps_sender_room_and_safe_reply_instruction():
    prompt = prompt_for(
        "!room:example.test",
        "@owner:example.test",
        "hello from Element",
    )
    assert "[Matrix message from human" in prompt
    assert "@owner:example.test" in prompt
    assert "hello from Element" in prompt
    assert "notify --matrix '!room:example.test' <<'WOLTSPACE_NOTIFY_" in prompt


def test_allowlist_and_device_pins_are_exact(monkeypatch):
    monkeypatch.setenv(
        "MATRIX_ALLOWED_USERS",
        "@owner:example.test,@second:example.test",
    )
    monkeypatch.setenv(
        "MATRIX_TRUSTED_DEVICES",
        "@owner:example.test|PHONE,@owner:example.test|PWA,broken",
    )
    assert allowed_users() == {"@owner:example.test", "@second:example.test"}
    assert trusted_devices() == {
        ("@owner:example.test", "PHONE"),
        ("@owner:example.test", "PWA"),
    }


class FakeRoom:
    room_id = "!room:example.test"
    encrypted = True


class FakeEvent:
    sender = "@owner:example.test"
    body = "hello from Element"
    decrypted = True
    verified = True


@pytest.mark.asyncio
async def test_decrypted_allowed_message_enters_existing_wolt_session(monkeypatch):
    adapter = MatrixAdapter.__new__(MatrixAdapter)
    adapter.room_id = FakeRoom.room_id
    adapter.user_id = "@n00b:example.test"
    adapter.allowed = {FakeEvent.sender}
    delivered = {}
    monkeypatch.setattr(adapter_module, "resolve_active_session", lambda _wolt: "n00b-session")
    monkeypatch.setattr(
        adapter_module,
        "deliver_message",
        lambda session, text: delivered.update(session=session, text=text) or {"status": "delivered"},
    )
    monkeypatch.setattr(adapter_module, "_bot_log", lambda *_args: None)
    adapter.wolt = "n00b"
    await adapter.on_message(FakeRoom(), FakeEvent())
    assert delivered["session"] == "n00b-session"
    assert "hello from Element" in delivered["text"]
    assert "notify --matrix" in delivered["text"]


@pytest.mark.asyncio
async def test_untrusted_sender_or_unencrypted_message_never_enters_session(monkeypatch):
    adapter = MatrixAdapter.__new__(MatrixAdapter)
    adapter.room_id = FakeRoom.room_id
    adapter.user_id = "@n00b:example.test"
    adapter.allowed = {"@someone-else:example.test"}
    adapter.wolt = "n00b"
    monkeypatch.setattr(
        adapter_module,
        "deliver_message",
        lambda *_args: pytest.fail("untrusted message reached the session"),
    )
    await adapter.on_message(FakeRoom(), FakeEvent())
    adapter.allowed = {FakeEvent.sender}
    event = FakeEvent()
    event.decrypted = False
    await adapter.on_message(FakeRoom(), event)
    event.decrypted = True
    event.verified = False
    await adapter.on_message(FakeRoom(), event)
