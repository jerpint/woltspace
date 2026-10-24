"""A file, a voice note or a spoken message goes only to the lodge's own chats. Text goes anywhere."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container" / "lib"))

import outbox
import server.app as server_app
from server import notify
from server.notification_types import Plugin

OGG = b"OggS" + b"\x00" * 23
WORDS = {"text": "The build is green."}
NOTIFY_CHANNEL = "C0NOTIFY001"


def _refusal(adapter: str, chat: str) -> str:
    return (
        f"a file goes only to this lodge's own chats, and {adapter} {chat} is not one of "
        "them; send it to your own chat, or send text only"
    )


def _recorder(sent, adapter):
    async def send(message, destination, context):
        sent.append(SimpleNamespace(
            adapter=adapter, message=message, destination=dict(destination),
            route_state=dict(context.route_state),
        ))
    return send


@pytest.fixture
def lodge(monkeypatch, tmp_path):
    env = {
        "TELEGRAM_BOT_TOKEN": "telegram-token", "TELEGRAM_ALLOWED_USERS": "101,202",
        "SLACK_BOT_TOKEN": "slack-token", "SLACK_NOTIFY_CHANNEL": NOTIFY_CHANNEL,
        "OPENAI_API_KEY": "sk-test",
    }
    state = SimpleNamespace(
        box=tmp_path / "outbox", env=env, sent=[], history=Mock(), routing=None,
        tmp=tmp_path, spoken=[],
    )

    async def synthesize(text, speech_format, target, **settings):
        state.spoken.append(text)
        target.write_bytes(OGG)
        return len(OGG)

    monkeypatch.setattr(notify, "SPACE_PLATFORM_DIR", tmp_path)
    monkeypatch.setattr(notify, "OUTBOX_DIR", state.box)
    monkeypatch.setattr(notify, "WOLTS_DIR", tmp_path)
    monkeypatch.setattr(notify, "dotenv_env", lambda key: env.get(key, ""))
    monkeypatch.setattr(notify, "read_session_registry", lambda name: state.routing)
    monkeypatch.setattr(notify, "append_chat_history", state.history)
    monkeypatch.setattr(notify, "SessionRegistry", lambda root: Mock())
    monkeypatch.setattr(notify, "synthesize", synthesize)
    for adapter, secret, route_state, speech_format in (
        ("telegram", "TELEGRAM_BOT_TOKEN", (), "opus"),
        ("slack", "SLACK_BOT_TOKEN", ("slack_progress_mode", "slack_progress_ts"), "mp3"),
    ):
        monkeypatch.setitem(notify.SENDERS, adapter, Plugin(
            adapter, _recorder(state.sent, adapter), secrets=(secret,),
            route_state=route_state, max_attachment_bytes=50 * 1024 * 1024,
            speech_format=speech_format,
        ))
    return state


def _file(lodge, name="report.html", kind=None) -> list[dict]:
    source = lodge.tmp / name
    source.write_bytes(OGG)
    entry = {"id": outbox.stage(source, lodge.box), "name": name}
    if kind:
        entry["kind"] = kind
    return [entry]


def _telegram(chat) -> dict:
    return {"adapter": "telegram", "chat_id": chat}


def _slack(channel, thread="1.2") -> dict:
    return {"adapter": "slack", "channel": channel, "thread_ts": thread}


def _left_in_the_outbox(lodge) -> list:
    return sorted(lodge.box.iterdir()) if lodge.box.exists() else []


async def _refused(lodge, explicit, attachments=(), speak=None) -> notify.AttachmentError:
    with pytest.raises(notify.AttachmentError) as error:
        await notify.send_notification("test-session", "hi\n", explicit, attachments, speak)
    assert error.value.reason == "attachment_destination"
    assert lodge.sent == []
    assert lodge.spoken == []
    lodge.history.assert_not_called()
    assert _left_in_the_outbox(lodge) == []
    return error.value


# --- Telegram: the chats a file may go to -------------------------------------

@pytest.mark.parametrize("allowed, chat", [
    ("101,202", "101"), ("101,202", "202"), (" 101 , 202 ", "202"), ("101,202", 202),
])
@pytest.mark.asyncio
async def test_a_file_goes_to_a_chat_in_the_allowed_users(lodge, allowed, chat):
    lodge.env["TELEGRAM_ALLOWED_USERS"] = allowed

    await notify.send_notification("test-session", "hi\n", _telegram(chat), _file(lodge))

    (sent,) = lodge.sent
    assert sent.destination == {"chat_id": str(chat)}
    assert len(sent.message.attachments) == 1


@pytest.mark.parametrize("allowed", ["101,202", "", None])
@pytest.mark.parametrize("recorded, chat", [("-100777", "-100777"), (-100777, "-100777"), ("303", 303)])
@pytest.mark.asyncio
async def test_a_file_goes_to_the_session_s_own_telegram_chat_whatever_the_list_says(
    lodge, allowed, recorded, chat,
):
    if allowed is None:
        del lodge.env["TELEGRAM_ALLOWED_USERS"]
    else:
        lodge.env["TELEGRAM_ALLOWED_USERS"] = allowed
    lodge.routing = {"adapter": "telegram", "chat_id": recorded}

    await notify.send_notification("test-session", "hi\n", _telegram(chat), _file(lodge))

    assert lodge.sent[0].destination == {"chat_id": str(chat)}


@pytest.mark.asyncio
async def test_the_session_route_and_the_default_route_carry_a_file(lodge):
    await notify.send_notification("test-session", "hi\n", None, _file(lodge))      # first allowed user
    lodge.routing = {"adapter": "telegram", "chat_id": "-100777"}                  # a group, in no list
    await notify.send_notification("test-session", "hi\n", None, _file(lodge))

    assert [sent.destination for sent in lodge.sent] == [{"chat_id": "101"}, {"chat_id": "-100777"}]


@pytest.mark.parametrize("allowed, routing", [
    ("101,202", None),
    ("", None),
    (None, None),
    ("101,202", {"adapter": "telegram", "chat_id": "-100777"}),      # the session's chat is another
    ("101,202", {"adapter": "slack", "chat_id": "999"}),             # the same id on another adapter
    ("101,202", {"adapter": "telegram", "chat_id": ""}),
    ("101,202", {"adapter": "telegram"}),
    ("1010,9999", None),                                             # no entry is the chat itself
])
@pytest.mark.asyncio
async def test_a_file_to_any_other_telegram_chat_is_refused_and_removed(lodge, allowed, routing):
    if allowed is None:
        del lodge.env["TELEGRAM_ALLOWED_USERS"]
    else:
        lodge.env["TELEGRAM_ALLOWED_USERS"] = allowed
    lodge.routing = routing

    error = await _refused(lodge, _telegram("999"), _file(lodge))

    assert str(error) == _refusal("telegram", "999")


# --- Slack: the channels a file may go to --------------------------------------

@pytest.mark.parametrize("channel", [NOTIFY_CHANNEL, ""])
@pytest.mark.asyncio
async def test_a_file_goes_to_the_notify_channel_named_or_by_default(lodge, channel):
    await notify.send_notification("test-session", "hi\n", _slack(channel), _file(lodge))

    assert lodge.sent[0].destination == {"channel": NOTIFY_CHANNEL, "thread_ts": "1.2"}


@pytest.mark.parametrize("notify_channel", [NOTIFY_CHANNEL, ""])
@pytest.mark.parametrize("thread, exact", [("1.2", True), ("9.9", False)])
@pytest.mark.asyncio
async def test_a_file_goes_to_the_session_s_own_slack_chat_in_any_thread(
    lodge, notify_channel, thread, exact,
):
    lodge.env["SLACK_NOTIFY_CHANNEL"] = notify_channel
    lodge.routing = {
        "adapter": "slack", "chat_id": "D0SESSION01", "thread_ts": "1.2",
        "slack_progress_mode": "agent",
    }

    await notify.send_notification(
        "test-session", "hi\n", _slack("D0SESSION01", thread), _file(lodge),
    )

    (sent,) = lodge.sent
    assert sent.destination == {"channel": "D0SESSION01", "thread_ts": thread}
    # The session's record vouches for the chat in any thread, but its progress
    # state still belongs to the one thread the record names.
    assert sent.route_state == ({"slack_progress_mode": "agent"} if exact else {})


@pytest.mark.asyncio
async def test_the_slack_session_route_carries_a_file(lodge):
    lodge.env["SLACK_NOTIFY_CHANNEL"] = ""
    lodge.routing = {"adapter": "slack", "chat_id": "D0SESSION01", "thread_ts": "1.2"}

    await notify.send_notification("test-session", "hi\n", None, _file(lodge))

    assert lodge.sent[0].destination == {"channel": "D0SESSION01", "thread_ts": "1.2"}


@pytest.mark.parametrize("notify_channel, routing", [
    (NOTIFY_CHANNEL, None),
    ("", None),
    (None, None),
    (NOTIFY_CHANNEL, {"adapter": "slack", "chat_id": "D0SESSION01"}),      # another chat
    (NOTIFY_CHANNEL, {"adapter": "telegram", "chat_id": "C0OTHER0001"}),   # another adapter
    (NOTIFY_CHANNEL, {"adapter": "slack", "chat_id": ""}),
])
@pytest.mark.asyncio
async def test_a_file_to_any_other_slack_channel_is_refused_and_removed(
    lodge, notify_channel, routing,
):
    if notify_channel is None:
        del lodge.env["SLACK_NOTIFY_CHANNEL"]
    else:
        lodge.env["SLACK_NOTIFY_CHANNEL"] = notify_channel
    lodge.routing = routing

    error = await _refused(lodge, _slack("C0OTHER0001"), _file(lodge))

    assert str(error) == _refusal("slack", "C0OTHER0001")


# --- everything that is not text -------------------------------------------------

@pytest.mark.parametrize("explicit, adapter, chat", [
    (_telegram("999"), "telegram", "999"), (_slack("C0OTHER0001"), "slack", "C0OTHER0001"),
])
@pytest.mark.asyncio
async def test_a_voice_note_to_another_chat_is_refused(lodge, explicit, adapter, chat):
    error = await _refused(lodge, explicit, _file(lodge, "note.ogg", "voice"))

    assert str(error) == _refusal(adapter, chat)


@pytest.mark.parametrize("explicit, adapter, chat", [
    (_telegram("999"), "telegram", "999"), (_slack("C0OTHER0001"), "slack", "C0OTHER0001"),
])
@pytest.mark.asyncio
async def test_a_spoken_message_to_another_chat_is_refused_before_anything_is_spoken(
    lodge, explicit, adapter, chat,
):
    error = await _refused(lodge, explicit, (), WORDS)

    assert str(error) == _refusal(adapter, chat)


@pytest.mark.parametrize("explicit", [_telegram("202"), _slack(NOTIFY_CHANNEL)])
@pytest.mark.asyncio
async def test_a_spoken_message_to_one_of_the_lodge_s_chats_is_spoken(lodge, explicit):
    await notify.send_notification("test-session", "hi\n", explicit, (), WORDS)

    assert lodge.spoken == ["The build is green."]
    assert lodge.sent[0].message.attachments[0].kind == "voice"


@pytest.mark.parametrize("explicit, destination", [
    (_telegram("999"), {"chat_id": "999"}),
    (_slack("C0OTHER0001"), {"channel": "C0OTHER0001", "thread_ts": "1.2"}),
])
@pytest.mark.asyncio
async def test_text_still_goes_to_a_chat_outside_every_list(lodge, explicit, destination):
    lodge.env.update(TELEGRAM_ALLOWED_USERS="", SLACK_NOTIFY_CHANNEL="")

    result = await notify.send_notification("test-session", "hi\n", explicit)

    (sent,) = lodge.sent
    assert sent.destination == destination
    assert sent.message.attachments == ()
    assert "attachments" not in result


# --- where the check sits ------------------------------------------------------

@pytest.mark.parametrize("explicit, secret", [
    (_telegram("999"), "TELEGRAM_BOT_TOKEN"), (_slack("C0OTHER0001"), "SLACK_BOT_TOKEN"),
])
@pytest.mark.asyncio
async def test_a_missing_token_is_reported_before_the_destination(lodge, explicit, secret):
    lodge.env[secret] = ""

    with pytest.raises(RuntimeError, match=f"^{secret} not set$"):
        await notify.send_notification("test-session", "hi\n", explicit, _file(lodge))

    assert lodge.sent == []


@pytest.mark.asyncio
async def test_the_destination_is_refused_before_the_size_is_looked_at(lodge, monkeypatch):
    monkeypatch.setitem(notify.SENDERS, "telegram", Plugin(
        "telegram", _recorder(lodge.sent, "telegram"), secrets=("TELEGRAM_BOT_TOKEN",),
        max_attachment_bytes=4,
    ))

    error = await _refused(lodge, _telegram("999"), _file(lodge))

    assert str(error) == _refusal("telegram", "999")


# --- the route -------------------------------------------------------------------

def test_the_route_answers_400_with_the_reason(lodge, monkeypatch):
    # The real send_notification runs here. The outbound transports are
    # replaced and must stay unreached: core refuses the destination first.
    telegram_send, telegram_send_document = AsyncMock(), AsyncMock()
    monkeypatch.setattr(notify, "telegram_send", telegram_send)
    monkeypatch.setattr(notify, "telegram_send_document", telegram_send_document)
    monkeypatch.setattr(server_app, "bot_log", Mock())
    attachments = _file(lodge)

    response = TestClient(server_app.app, base_url="http://localhost:7777").post(
        "/notify",
        json={
            "session": "test-session", "message": "hi", "adapter": "telegram",
            "chat_id": "999", "attachments": attachments,
        },
    )

    assert response.status_code == 400
    assert response.json() == {
        "ok": False, "error": _refusal("telegram", "999"), "reason": "attachment_destination",
    }
    telegram_send.assert_not_awaited()
    telegram_send_document.assert_not_awaited()
    assert lodge.sent == []
    assert _left_in_the_outbox(lodge) == []
