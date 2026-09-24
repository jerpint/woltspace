"""Fail-closed Slack DM admission and response transport."""

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "container"))

from bot import slack_adapter as slack  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_admission(monkeypatch, tmp_path):
    monkeypatch.setenv("SLACK_OWNER_USER", "U12345678")
    monkeypatch.setattr(slack, "CHAT_DIR", tmp_path)
    monkeypatch.setattr(slack, "OWNER_SELECTIONS_FILE", tmp_path / "owners.json")
    monkeypatch.setattr(slack, "THREAD_SESSIONS_FILE", tmp_path / "threads.json")
    monkeypatch.setattr(slack, "PENDING_MESSAGES_FILE", tmp_path / "pending.json")
    monkeypatch.setattr(slack, "PENDING_LOCK_FILE", tmp_path / "pending.lock")
    slack._seen_event_ids.clear()
    slack._seen_event_order.clear()
    slack._owner_selections.clear()
    slack._thread_sessions.clear()
    slack._active_threads.clear()


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


class FakeApp:
    def __init__(self):
        self.handlers = {}

    def event(self, event_type):
        def register(handler):
            self.handlers[event_type] = handler
            return handler
        return register

    def action(self, action_id):
        return self.event(f"action:{action_id}")


def install_fake_app(monkeypatch):
    app = FakeApp()
    monkeypatch.setattr(slack, "AsyncApp", lambda **kwargs: app)
    slack.create_app()
    return app


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

        def action(self, action_id):
            return self.event(f"action:{action_id}")

    fake_app = FakeApp()
    monkeypatch.setattr(slack, "AsyncApp", lambda **kwargs: fake_app)
    extract = Mock(side_effect=AssertionError("attachment work must not run"))
    history = AsyncMock(side_effect=AssertionError("history must not run"))
    response = Mock(side_effect=AssertionError("model/session work must not run"))
    monkeypatch.setattr(slack, "_extract_image", extract)
    monkeypatch.setattr(slack, "_build_thread_context", history)
    monkeypatch.setattr(slack, "start_claude_session", response)
    slack.create_app()

    assert set(fake_app.handlers) == {"message", "action:select_wolt"}
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
    client.chat_postMessage.return_value = {"ok": True, "ts": "2000.1"}
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
    client.chat_postMessage.return_value = {"ok": True, "ts": "2000.1"}
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


def test_selection_storage_is_atomic_private_and_owner_keyed(monkeypatch, tmp_path):
    monkeypatch.setattr(slack, "CHAT_DIR", tmp_path)
    path = tmp_path / "_owner_selections.json"
    monkeypatch.setattr(slack, "OWNER_SELECTIONS_FILE", path)

    slack._save_owner_selections({"U12345678": "n00b"})

    assert path.stat().st_mode & 0o777 == 0o600
    assert path.read_text() == '{"U12345678": "n00b"}\n'
    assert list(tmp_path.glob("*.tmp")) == []


def test_picker_caps_static_select_deterministically_but_text_lists_all():
    wolts = {f"wolt-{index:03}": {"name": f"wolt-{index:03}"} for index in range(105)}
    blocks = slack._picker_blocks(wolts)
    options = blocks[0]["elements"][0]["options"]

    assert len(options) == 100
    assert options[0]["value"] == "wolt-000"
    assert options[-1]["value"] == "wolt-099"
    assert "105. wolt-104" in slack._picker_text(wolts)


def test_pending_storage_is_private_atomic_and_claim_removes_plaintext():
    slack._pending_create("U12345678", "D1", "1000.1", "Ev1", "private task", now=1)
    slack._pending_set_picker("D1", "1000.1", "2000.1")

    claimed = slack._pending_claim("U12345678", "D1", "2000.1", "n00b", now=2)
    stored = json.loads(slack.PENDING_MESSAGES_FILE.read_text())["D1:1000.1"]

    assert claimed["text"] == "private task"
    assert stored["state"] == "claimed"
    assert "text" not in stored
    assert slack.PENDING_MESSAGES_FILE.stat().st_mode & 0o777 == 0o600
    assert slack.PENDING_LOCK_FILE.stat().st_mode & 0o777 == 0o600
    assert list(slack.CHAT_DIR.glob("*.tmp")) == []


def test_pending_claim_expires_and_rejects_wrong_binding():
    slack._pending_create("U12345678", "D1", "1000.1", "Ev1", "task", now=1)
    slack._pending_set_picker("D1", "1000.1", "2000.1")

    assert slack._pending_claim("U-other", "D1", "2000.1", "n00b", now=2) is None
    assert slack._pending_claim("U12345678", "D-other", "2000.1", "n00b", now=2) is None
    assert slack._pending_claim("U12345678", "D1", "wrong", "n00b", now=2) is None
    assert slack._pending_claim("U12345678", "D1", "2000.1", "n00b", now=602) is None
    stored = json.loads(slack.PENDING_MESSAGES_FILE.read_text())["D1:1000.1"]
    assert stored["state"] == "expired"
    assert "text" not in stored


def test_pending_claim_race_has_one_winner_and_never_replays():
    slack._pending_create("U12345678", "D1", "1000.1", "Ev1", "task", now=1)
    slack._pending_set_picker("D1", "1000.1", "2000.1")

    def claim(_):
        return slack._pending_claim("U12345678", "D1", "2000.1", "n00b", now=2)

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(claim, range(12)))

    assert sum(result is not None for result in results) == 1
    assert slack._pending_claim("U12345678", "D1", "2000.1", "n00b", now=3) is None


def test_ambiguous_pending_recovery_never_replays():
    slack.PENDING_MESSAGES_FILE.write_text('{"D1:1000.1":')

    assert slack._pending_claim("U12345678", "D1", "2000.1", "n00b", now=2) is None
    assert json.loads(slack.PENDING_MESSAGES_FILE.read_text()) == {}


def test_pending_caps_and_message_size_are_bounded():
    for index in range(slack.PENDING_MAX_PER_OWNER):
        slack._pending_create(
            "U12345678", "D1", f"1000.{index}", f"Ev{index}", "task", now=1
        )
    with pytest.raises(ValueError, match="too many pending"):
        slack._pending_create("U12345678", "D1", "2000.1", "EvX", "task", now=2)
    with pytest.raises(ValueError, match="1..32768"):
        slack._pending_create("U-other", "D1", "3000.1", "EvY", "x" * 32769, now=2)


@pytest.mark.asyncio
async def test_no_selection_shows_picker_without_replaying_or_agent_work(monkeypatch):
    app = install_fake_app(monkeypatch)
    eligible = {"n00b": {"name": "n00b", "type": "raccoon"}}
    monkeypatch.setattr(slack, "_eligible_wolts", lambda: eligible)
    extract = Mock(side_effect=AssertionError("attachment work must not run"))
    start = Mock(side_effect=AssertionError("session work must not run"))
    history = AsyncMock(side_effect=AssertionError("history must not run"))
    monkeypatch.setattr(slack, "_extract_image", extract)
    monkeypatch.setattr(slack, "start_claude_session", start)
    monkeypatch.setattr(slack, "_build_thread_context", history)
    client = AsyncMock()
    client.chat_postMessage.return_value = {"ok": True, "ts": "2000.1"}

    await app.handlers["message"](
        event(text="private first message"), client, {}, {"event_id": "EvPicker"}
    )

    sent = client.chat_postMessage.await_args.kwargs
    assert "Choose your wolt" in sent["text"]
    assert "private first message" not in str(sent)
    extract.assert_not_called()
    start.assert_not_called()
    history.assert_not_awaited()


@pytest.mark.asyncio
async def test_bare_top_level_wolt_reopens_picker_without_spawning_or_replaying(monkeypatch):
    app = install_fake_app(monkeypatch)
    slack._owner_selections["U12345678"] = "n00b"
    eligible = {"n00b": {"name": "n00b", "type": "raccoon"}}
    monkeypatch.setattr(slack, "_eligible_wolts", lambda: eligible)
    start = Mock(side_effect=AssertionError("session work must not run"))
    extract = Mock(side_effect=AssertionError("attachment work must not run"))
    history = AsyncMock(side_effect=AssertionError("history must not run"))
    monkeypatch.setattr(slack, "start_claude_session", start)
    monkeypatch.setattr(slack, "_extract_image", extract)
    monkeypatch.setattr(slack, "_build_thread_context", history)
    client = AsyncMock()

    await app.handlers["message"](
        event(text="wolt"), client, {}, {"event_id": "EvReopenPicker"}
    )

    sent = client.chat_postMessage.await_args.kwargs
    assert "Choose your wolt" in sent["text"]
    assert "Current selection: `n00b`" in sent["text"]
    assert sent["blocks"] == slack._picker_blocks(eligible)
    start.assert_not_called()
    extract.assert_not_called()
    history.assert_not_awaited()


@pytest.mark.asyncio
async def test_bare_wolt_inside_owned_thread_stays_with_historical_session(monkeypatch):
    app = install_fake_app(monkeypatch)
    slack._thread_sessions["D12345678:1000.1"] = {
        "session": "old-session-1", "wolt": "old-wolt", "creature": "otter"
    }
    routed = AsyncMock()
    monkeypatch.setattr(slack, "_route_to_session", routed)
    client = AsyncMock()

    await app.handlers["message"](
        event(text="wolt", thread_ts="1000.1", event_ts="2000.1", ts="2000.1"),
        client, {}, {"event_id": "EvThreadWolt"},
    )

    routed.assert_awaited_once()
    assert routed.await_args.args[3]["session"] == "old-session-1"
    client.chat_postMessage.assert_not_awaited()


@pytest.mark.asyncio
async def test_prior_selection_still_opens_picker_without_starting(monkeypatch):
    app = install_fake_app(monkeypatch)
    slack._owner_selections["U12345678"] = "builder"
    monkeypatch.setattr(slack, "_eligible_wolts", lambda: {
        "builder": {"name": "builder", "type": "beaver"},
        "other": {"name": "other", "type": "raccoon"},
    })
    monkeypatch.setattr(slack, "_extract_image", lambda incoming: None)
    start = Mock(side_effect=AssertionError("must wait for selection"))
    monkeypatch.setattr(slack, "start_claude_session", start)
    client = AsyncMock()
    client.chat_postMessage.return_value = {"ok": True, "ts": "2000.1"}

    await app.handlers["message"](
        event(text="do the work"), client, {}, {"event_id": "EvStart"}
    )

    start.assert_not_called()
    assert "Choose your wolt" in client.chat_postMessage.await_args.kwargs["text"]
    pending = json.loads(slack.PENDING_MESSAGES_FILE.read_text())
    assert pending["D12345678:1234.5678"]["text"] == "do the work"


@pytest.mark.asyncio
async def test_attachment_is_explicitly_deferred(monkeypatch):
    app = install_fake_app(monkeypatch)
    slack._owner_selections["U12345678"] = "builder"
    monkeypatch.setattr(slack, "_eligible_wolts", lambda: {
        "builder": {"name": "builder", "type": "beaver"}
    })
    monkeypatch.setattr(slack, "_extract_image", lambda incoming: None)

    client = AsyncMock()

    await app.handlers["message"](
        event(text="see file", files=[{"id": "F1"}]), client, {}, {"event_id": "EvFile"}
    )

    assert client.chat_postMessage.await_count == 1
    assert "Attachments are not supported" in client.chat_postMessage.await_args.kwargs["text"]
    assert not slack.PENDING_MESSAGES_FILE.exists()


@pytest.mark.asyncio
async def test_failed_session_watcher_deletes_and_clears_progress(monkeypatch):
    monkeypatch.setattr(slack.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(slack.registry, "get", Mock(return_value={
        "status": "failed", "slack_progress_ts": "2000.1"
    }))
    update = Mock()
    monkeypatch.setattr(slack.registry, "update", update)
    client = AsyncMock()

    await slack._watch_progress(
        client, "builder-session-1", "builder", "D123"
    )

    client.chat_delete.assert_awaited_once_with(channel="D123", ts="2000.1")
    update.assert_called_once_with(
        "builder-session-1", wolt="builder",
        slack_progress_mode="", slack_progress_ts="",
    )


@pytest.mark.asyncio
async def test_progress_heartbeat_is_bounded_to_thirty_second_cadence(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(slack.asyncio, "sleep", sleep)
    monkeypatch.setattr(slack.registry, "get", Mock(return_value={
        "status": "running", "slack_progress_ts": "2000.1"
    }))
    client = AsyncMock()
    client.chat_update.side_effect = RuntimeError("rate limited")

    await slack._watch_progress(client, "builder-session-1", "builder", "D123")

    sleep.assert_awaited_once_with(30)
    client.chat_update.assert_awaited_once_with(
        channel="D123", ts="2000.1", text="🦫 Still working… 30s"
    )


@pytest.mark.asyncio
async def test_spawn_pending_delivers_original_and_pins_root(monkeypatch):
    session = {"name": "n00b-session-1"}
    start = Mock(return_value=session)
    update = Mock()
    watcher = AsyncMock()
    monkeypatch.setattr(slack, "start_claude_session", start)
    monkeypatch.setattr(slack.registry, "update", update)
    monkeypatch.setattr(slack, "_watch_progress", watcher)
    client = AsyncMock()
    selected = {"name": "n00b", "type": "raccoon"}
    claimed = {
        "user": "U12345678", "channel": "D1", "root_ts": "1000.1",
        "picker_ts": "2000.1", "text": "original task",
    }

    await slack._spawn_pending(client, selected, claimed)

    start.assert_called_once()
    first = start.call_args
    assert first.args == ("original task",)
    assert first.kwargs["wolt"] == "n00b"
    assert first.kwargs["routing"] == {
        "adapter": "slack", "chat_id": "D1", "thread_ts": "1000.1"
    }
    assert slack._thread_sessions["D1:1000.1"]["session"] == "n00b-session-1"
    assert client.chat_update.await_args_list[0].kwargs["text"].startswith("✅ Accepted")
    assert "🦫 Working" in client.chat_update.await_args_list[1].kwargs["text"]


@pytest.mark.asyncio
async def test_historical_owned_thread_is_not_retargeted_after_selection(monkeypatch):
    app = install_fake_app(monkeypatch)
    slack._owner_selections["U12345678"] = "new-wolt"
    slack._thread_sessions["D12345678:1000.1"] = {
        "session": "old-wolt-session-1", "wolt": "old-wolt", "creature": "otter"
    }
    routed = AsyncMock()
    monkeypatch.setattr(slack, "_route_to_session", routed)
    monkeypatch.setattr(
        slack, "start_claude_session", Mock(side_effect=AssertionError("must not retarget"))
    )

    await app.handlers["message"](
        event(thread_ts="1000.1", event_ts="2000.1", ts="2000.1"),
        AsyncMock(), {}, {"event_id": "EvOldThread"},
    )

    assert routed.await_args.args[3]["wolt"] == "old-wolt"


@pytest.mark.asyncio
async def test_static_select_revalidates_owner_and_live_option(monkeypatch):
    app = install_fake_app(monkeypatch)
    monkeypatch.setattr(slack, "_eligible_wolts", lambda: {
        "n00b": {"name": "n00b", "type": "raccoon"}
    })
    handler = app.handlers["action:select_wolt"]
    ack = AsyncMock()
    client = AsyncMock()
    spawn = AsyncMock()
    monkeypatch.setattr(slack, "_spawn_pending", spawn)
    slack._pending_create(
        "U12345678", "D12345678", "1000.1", "EvPending", "original task", now=1
    )
    slack._pending_set_picker("D12345678", "1000.1", "2000.1")
    base = {
        "user": {"id": "U12345678"},
        "channel": {"id": "D12345678"},
        "message": {"ts": "2000.1"},
        "actions": [{"selected_option": {"value": "n00b"}}],
    }

    with patch.object(slack.time, "time", return_value=2):
        await handler(ack, base, client)
    assert slack._owner_selections == {"U12345678": "n00b"}
    assert spawn.await_args.args[2]["text"] == "original task"
    ack.assert_awaited_once()

    await handler(AsyncMock(), base, client)
    assert spawn.await_count == 1

    slack._owner_selections.clear()
    await handler(AsyncMock(), {**base, "user": {"id": "U87654321"}}, AsyncMock())
    assert slack._owner_selections == {}

    await handler(AsyncMock(), {
        **base, "actions": [{"selected_option": {"value": "removed-wolt"}}]
    }, AsyncMock())
    assert slack._owner_selections == {}
