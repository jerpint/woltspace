"""Telegram over the real socket: the failures that matter before the lodge's main channel moves.

The Telegram connector module and the lodge's ChatLodge, each on its own side
of a real unix socket (connector_client, connector_broker), with Telegram and
the lodge's sessions faked at their edges. Covered: delivery, a duplicate
update, a lodge restart with an event in flight, a stale launch's token, a
send whose answer never comes, a connector that drops while the lodge keeps
serving, files from the connector's own folder only, files out of the outbox
only, and the lodge's own owner check.
"""

import asyncio
import shutil
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
for part in ("container", "container/lib", "container/connectors/telegram"):
    sys.path.insert(0, str(ROOT / part))

import chat_lodge  # noqa: E402
import connector_broker  # noqa: E402
import connector_client  # noqa: E402
import connector_registry  # noqa: E402
import telegram_connector as tg  # noqa: E402

OWNER = 111
CHAT = 4242
LIVE = "pal-live-grove-000001"


@pytest.fixture
def world(monkeypatch):
    root = Path(tempfile.mkdtemp(prefix="wsx", dir="/tmp"))
    wolts = root / "w"
    (wolts / "pal" / "wolt").mkdir(parents=True)
    (wolts / "pal" / "wolt" / "wolt.json").write_text('{"type": "raccoon"}')
    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(wolts))
    side = chat_lodge.ChatLodge(connector_registry.spec("telegram"), [str(OWNER)])
    sent = []

    def message_session(name, text):
        sent.append((name, text))
        return {"ok": name == LIVE, "url": ""}

    for name, value in [("message_session", message_session), ("get_tunnel_url", lambda: ""),
                        ("dog_available", lambda: False), ("_bot_log", lambda *a: None),
                        ("transcribe_audio", lambda path: f"heard {Path(path).name}"),
                        ("resolve_active_session", lambda wolt: None),
                        ("list_wolts", lambda: [{"name": "pal", "type": "raccoon"}])]:
        monkeypatch.setattr(chat_lodge, name, value)
    side._save_chat_state(str(CHAT), {"active_wolt": "pal", "active_session": LIVE})
    monkeypatch.setattr(tg, "ALLOWED_USERS", {OWNER})
    monkeypatch.setattr(tg, "PENDING", type(tg.PENDING)())
    monkeypatch.setattr(tg, "DATA_DIR", wolts / ".space" / "telegram")
    monkeypatch.setattr(tg, "UPLOADS_DIR", wolts / ".space" / "telegram" / "uploads")
    monkeypatch.setattr(tg, "APP", MagicMock())
    monkeypatch.setenv("WOLTSPACE_CONNECTOR_OUTBOX", str(wolts / ".space" / "outbox"))
    seen = set()

    def dedup(name, event_id):
        fresh = (name, event_id) not in seen
        seen.add((name, event_id))
        return fresh

    run_dir = wolts / ".space" / "run" / "connectors"

    def broker():
        b = connector_broker.Broker(run_dir, dedup)
        b.add("telegram", side.handle, [("wolt", "switch")])
        return b

    def client(token):
        return connector_client.Client(
            "telegram", socket_path=str(connector_broker.socket_path(run_dir, "telegram")), token=token,
            on_say=tg.on_say, on_edit=tg.on_edit, on_ack=tg.on_ack, on_send=tg.on_send)

    yield {"wolts": wolts, "side": side, "sent": sent, "broker": broker, "client": client,
           "run_dir": run_dir}
    shutil.rmtree(root, ignore_errors=True)


def _update(update_id, text="hello", user=OWNER):
    update = MagicMock()
    update.update_id = update_id
    update.effective_user.id = user
    update.effective_chat.id = CHAT
    update.effective_chat.type = "private"
    update.message.text = text
    update.message.reply_to_message = None
    update.message.reply_text = AsyncMock()
    return update


def _context():
    context = MagicMock()
    context.bot.username = "lodgebot"
    context.bot.id = 777
    return context


async def _until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out")


def _said(update):
    return [c.args[0] for c in update.message.reply_text.await_args_list]


def test_a_message_is_delivered_once_even_when_telegram_sends_it_twice(world, monkeypatch):
    async def main():
        broker = world["broker"]()
        await broker.start()
        client = world["client"](connector_broker.new_token(world["run_dir"], "telegram"))
        monkeypatch.setattr(tg, "CLIENT", client)
        await client.connect()
        first, again = _update(7), _update(7)
        await tg.handle_message(first, _context())
        await tg.handle_message(again, _context())
        await broker.drain("telegram")
        await client.ping()
        assert [name for name, _ in world["sent"]] == [LIVE]
        assert _said(first) == [f"🪵 sent to {LIVE}\n{LIVE}"]
        assert _said(again) == []
        await client.close()
        await broker.stop()
    asyncio.run(main())


def test_an_event_in_flight_survives_a_lodge_restart(world, monkeypatch):
    async def main():
        broker = world["broker"]()
        await broker.start()
        client = world["client"](connector_broker.new_token(world["run_dir"], "telegram"))
        monkeypatch.setattr(tg, "CLIENT", client)
        await client.connect()
        await broker.stop()
        monkeypatch.setattr(connector_client, "RECEIPT_TIMEOUT", 0.2)
        update = _update(8, "while you were away")
        # The lodge is down: the connector keeps the event and says nothing yet.
        status = await client.event("message", {"chat_id": str(CHAT)}, str(OWNER),
                                    {"text": "while you were away"}, "tg-8", timeout=0.2)
        tg.PENDING["tg-8"] = update
        assert status == "pending" and world["sent"] == []
        broker = world["broker"]()
        await broker.start()
        await client.connect()                 # reconnect: what was not receipted goes again
        await client.ping()
        await broker.drain("telegram")
        await client.ping()
        assert [name for name, _ in world["sent"]] == [LIVE]
        assert "while you were away" in world["sent"][0][1]
        assert _said(update) == [f"🪵 sent to {LIVE}\n{LIVE}"]
        await client.close()
        await broker.stop()
    asyncio.run(main())


def test_a_connector_from_an_earlier_launch_is_turned_away(world):
    async def main():
        broker = world["broker"]()
        await broker.start()
        old = connector_broker.new_token(world["run_dir"], "telegram")
        connector_broker.new_token(world["run_dir"], "telegram")       # the supervisor relaunched
        with pytest.raises(connector_client.Refused):
            await world["client"](old).connect()
        assert not broker.connected("telegram")
        await broker.stop()
    asyncio.run(main())


def test_a_send_whose_answer_never_comes_is_ambiguous_and_not_retried(world, monkeypatch):
    async def main():
        broker = world["broker"]()
        await broker.start()
        client = world["client"](connector_broker.new_token(world["run_dir"], "telegram"))
        started = asyncio.Event()

        async def stuck(chat_id, text, **kwargs):
            started.set()
            await asyncio.Event().wait()
        tg.APP.bot.send_message = stuck
        await client.connect()
        sending = asyncio.create_task(broker.send("telegram", {"chat_id": str(CHAT)}, "message", "hi"))
        await asyncio.wait_for(started.wait(), 5)
        await client.close()                   # the connector dies mid-send
        with pytest.raises(connector_broker.SendFailed) as failed:
            await asyncio.wait_for(sending, 5)
        assert failed.value.status == "ambiguous"
        await broker.stop()
    asyncio.run(main())


def test_with_the_connector_gone_the_lodge_answers_at_once(world, monkeypatch):
    async def main():
        broker = world["broker"]()
        await broker.start()
        client = world["client"](connector_broker.new_token(world["run_dir"], "telegram"))
        await client.connect()
        await client.close()
        await _until(lambda: not broker.connected("telegram"))
        started = time.monotonic()
        with pytest.raises(connector_broker.ConnectorUnavailable):
            await broker.send("telegram", {"chat_id": str(CHAT)}, "message", "anyone?")
        assert time.monotonic() - started < 0.5
        # The lodge's loop is free: other work goes on.
        assert await asyncio.wait_for(asyncio.sleep(0, result="ok"), 0.5) == "ok"
        await broker.stop()
    asyncio.run(main())


def test_a_voice_note_and_a_file_reach_the_session_from_the_connector_s_folder(world, monkeypatch):
    async def main():
        broker = world["broker"]()
        await broker.start()
        client = world["client"](connector_broker.new_token(world["run_dir"], "telegram"))
        monkeypatch.setattr(tg, "CLIENT", client)
        monkeypatch.setattr(tg, "_download_bytes", AsyncMock(return_value=b"OggS"))
        monkeypatch.setattr(tg.random, "choice", lambda seq: seq[0])
        await client.connect()
        voice = _update(9)
        voice.message.voice.file_id, voice.message.voice.file_unique_id = "f", "u9"
        voice.message.caption = None
        await tg.handle_voice(voice, _context())
        doc = _update(10)
        doc.message.document.file_id, doc.message.document.file_unique_id = "f", "u10"
        doc.message.document.file_name, doc.message.document.mime_type = "../../notes.txt", "text/plain"
        doc.message.caption = "read this"
        await tg.handle_document(doc, _context())
        await broker.drain("telegram")
        await client.ping()
        texts = [text for _, text in world["sent"]]
        assert "[voice message] heard " in texts[0] and "voice_u9.ogg" in texts[0]
        uploads = world["wolts"] / ".space" / "telegram" / "uploads"
        assert "[file received] ../../notes.txt (text/plain, 4 bytes) saved at " + str(uploads) in texts[1]
        # Saved inside the connector's folder whatever name it came with.
        assert all(p.parent == uploads for p in uploads.iterdir())
        await client.close()
        await broker.stop()
    asyncio.run(main())


def test_an_attachment_outside_the_connector_s_folder_is_not_passed_on(world, monkeypatch):
    async def main():
        broker = world["broker"]()
        await broker.start()
        client = world["client"](connector_broker.new_token(world["run_dir"], "telegram"))
        said = []

        async def on_say(frame):
            said.append(frame["reply"]["text"])
        client.on_say = on_say
        await client.connect()
        status = await client.event("attachment", {"chat_id": str(CHAT)}, str(OWNER), {
            "media": "file", "path": "/etc/hosts", "name": "hosts", "mime": "text/plain", "size": 1,
        }, "tg-11")
        assert status == "accepted"
        await broker.drain("telegram")
        await client.ping()
        assert world["sent"] == []
        assert said == ["That file did not arrive where the lodge expects it, so it was not passed on."]
        await client.close()
        await broker.stop()
    asyncio.run(main())


def test_the_connector_sends_files_only_from_the_outbox(world):
    async def main():
        outbox = world["wolts"] / ".space" / "outbox"
        outbox.mkdir(parents=True)
        staged = outbox / "entry"
        staged.write_text("hi")
        (outbox / "link").symlink_to("/etc/hosts")
        tg.APP.bot.send_document = AsyncMock(return_value=MagicMock(message_id=5))
        ok = await tg.on_send({"route": {"chat_id": "1"}, "method": "document", "path": str(staged),
                               "filename": "a.txt", "content_type": "text/plain", "text": ""})
        assert ok == {"status": "delivered", "message_id": "5"}
        assert tg.APP.bot.send_document.await_args.kwargs["caption"] is None
        for path in ("/etc/hosts", str(outbox / "link")):
            refused = await tg.on_send({"route": {"chat_id": "1"}, "method": "document", "path": path,
                                        "filename": "a.txt", "content_type": "text/plain", "text": ""})
            assert refused["status"] == "refused"
        assert tg.APP.bot.send_document.await_count == 1
    asyncio.run(main())


@pytest.mark.parametrize("error, status", [
    ("BadRequest", "refused"), ("Forbidden", "refused"), ("NetworkError", "failed"), ("TimedOut", "ambiguous"),
])
def test_telegram_s_answers_become_send_results(world, error, status):
    import telegram.error as errors

    async def main():
        tg.APP.bot.send_message = AsyncMock(side_effect=getattr(errors, error)("no"))
        result = await tg.on_send({"route": {"chat_id": "1"}, "method": "message", "text": "hi"})
        assert result["status"] == status
    asyncio.run(main())


def test_the_lodge_checks_the_owner_itself(world, monkeypatch):
    async def main():
        broker = world["broker"]()
        await broker.start()
        client = world["client"](connector_broker.new_token(world["run_dir"], "telegram"))
        await client.connect()
        # A connector that let a stranger through: the lodge still says no.
        await client.event("message", {"chat_id": str(CHAT)}, "999", {"text": "let me in"}, "tg-12")
        await broker.drain("telegram")
        assert world["sent"] == []
        await client.close()
        await broker.stop()
    asyncio.run(main())
