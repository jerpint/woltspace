"""Fail-closed Slack DM admission and response transport."""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "container"))

from bot import slack_adapter as slack  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_admission(monkeypatch):
    monkeypatch.setenv("SLACK_OWNER_USER", "U12345678")
    slack._seen_event_ids.clear()
    slack._seen_event_order.clear()


def event(**changes):
    base = {
        "type": "message",
        "channel_type": "im",
        "channel": "D12345678",
        "user": "U12345678",
        "ts": "1234.5678",
        "event_ts": "1234.5678",
        "text": "hello",
    }
    return {**base, **changes}


class TestOwnerDmAdmission:
    @pytest.mark.parametrize("owner", ["", "jerpint", "C12345678", "U12 345678"])
    def test_missing_or_malformed_owner_fails_before_admission(self, monkeypatch, owner):
        monkeypatch.setenv("SLACK_OWNER_USER", owner)
        assert slack._accept_owner_dm(event(), {}) == (False, "owner_not_configured")

    def test_non_owner_is_rejected(self):
        assert slack._accept_owner_dm(event(user="U87654321"), {}) == (
            False, "not_owner"
        )

    @pytest.mark.parametrize("channel_type", ["channel", "group", "mpim"])
    def test_channel_and_app_mention_surfaces_are_rejected(self, channel_type):
        mention = event(type="app_mention", channel_type=channel_type)
        assert slack._accept_owner_dm(mention, {}) == (False, "not_dm")

    @pytest.mark.parametrize("changes", [{"bot_id": "B1"}, {"subtype": "message_changed"}])
    def test_bot_and_subtype_events_are_rejected(self, changes):
        assert slack._accept_owner_dm(event(**changes), {}) == (
            False, "non_human_event"
        )

    def test_duplicate_and_retried_events_are_claimed_once(self):
        body = {"event_id": "Ev123"}
        assert slack._accept_owner_dm(event(), body) == (True, "accepted")
        assert slack._accept_owner_dm(event(), body) == (False, "duplicate")

    def test_missing_stable_event_identity_fails_closed(self):
        incoming = event(ts=None, event_ts=None)
        assert slack._accept_owner_dm(incoming, {}) == (False, "missing_event_id")


@pytest.mark.asyncio
async def test_every_rejected_surface_returns_before_history_or_session_work(monkeypatch):
    class FakeApp:
        def __init__(self):
            self.handlers = {}

        def event(self, event_type):
            def register(handler):
                self.handlers[event_type] = handler
                return handler
            return register

    fake_app = FakeApp()
    monkeypatch.setattr(slack, "AsyncApp", lambda **kwargs: fake_app)
    extract = Mock(side_effect=AssertionError("attachment work must not run"))
    history = AsyncMock(side_effect=AssertionError("history must not run"))
    response = Mock(side_effect=AssertionError("model/session work must not run"))
    monkeypatch.setattr(slack, "_extract_image", extract)
    monkeypatch.setattr(slack, "_build_thread_context", history)
    monkeypatch.setattr(slack, "get_response", response)
    slack.create_app()

    assert set(fake_app.handlers) == {"message"}  # no app_mention listener
    handler = fake_app.handlers["message"]
    rejected = [
        (event(user="U87654321"), {"event_id": "EvOther"}),
        (event(type="app_mention", channel_type="channel"), {"event_id": "EvMention"}),
        (event(bot_id="B1"), {"event_id": "EvBot"}),
        (event(subtype="message_changed"), {"event_id": "EvSubtype"}),
    ]
    for incoming, body in rejected:
        await handler(incoming, AsyncMock(), {}, body)

    monkeypatch.setenv("SLACK_OWNER_USER", "not-a-member-id")
    await handler(event(), AsyncMock(), {}, {"event_id": "EvNoOwner"})
    monkeypatch.setenv("SLACK_OWNER_USER", "U12345678")

    # A retry is also rejected before downstream work.
    duplicate = event(event_ts="2000.1", ts="2000.1")
    duplicate_body = {"event_id": "EvDuplicate"}
    assert slack._accept_owner_dm(duplicate, duplicate_body) == (True, "accepted")
    await handler(duplicate, AsyncMock(), {}, duplicate_body)

    extract.assert_not_called()
    history.assert_not_awaited()
    response.assert_not_called()


@pytest.mark.asyncio
async def test_streaming_success_stops_the_stream_without_posting_fallback():
    client = AsyncMock()
    client.chat_startStream.return_value = {"ok": True, "ts": "2000.1"}

    await slack._post_text(client, "D1", "1000.1", "U12345678", "answer")

    client.chat_stopStream.assert_awaited_once_with(channel="D1", ts="2000.1")
    client.chat_postMessage.assert_not_awaited()


@pytest.mark.asyncio
async def test_streaming_failure_uses_plain_postmessage_fallback():
    client = AsyncMock()
    client.chat_startStream.side_effect = RuntimeError("not an agent app")

    await slack._post_text(client, "D1", "1000.1", "U12345678", "answer")

    client.chat_postMessage.assert_awaited_once_with(
        channel="D1", thread_ts="1000.1", text="answer"
    )
    client.chat_stopStream.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_route_reports_successful_revival(monkeypatch, tmp_path):
    async def revived(*args):
        return {"ok": True, "status": "revived", "url": "https://session.test"}

    monkeypatch.setattr(slack.asyncio, "to_thread", revived)
    monkeypatch.setattr(slack, "CHAT_DIR", tmp_path)
    client = AsyncMock()
    owner = {"session": "n00b-old-1", "wolt": "n00b", "creature": "raccoon"}

    await slack._route_to_session(client, "D1", "1000.1", owner, "continue")

    sent = client.chat_postMessage.await_args.kwargs["text"]
    assert "revived and delivered" in sent


@pytest.mark.asyncio
async def test_dead_session_releases_thread_back_to_dog(monkeypatch, tmp_path):
    async def dead(*args):
        return {"ok": False, "error": "gone"}

    monkeypatch.setattr(slack.asyncio, "to_thread", dead)
    monkeypatch.setattr(slack, "CHAT_DIR", tmp_path)
    monkeypatch.setattr(slack, "THREAD_SESSIONS_FILE", tmp_path / "owners.json")
    monkeypatch.setattr(slack, "_thread_sessions", {
        "D1:1000.1": {"session": "n00b-old-1", "wolt": "n00b", "creature": "raccoon"}
    })
    client = AsyncMock()
    owner = slack._thread_sessions["D1:1000.1"]

    await slack._route_to_session(client, "D1", "1000.1", owner, "continue")

    assert "D1:1000.1" not in slack._thread_sessions
    assert "send again" in client.chat_postMessage.await_args.kwargs["text"]
