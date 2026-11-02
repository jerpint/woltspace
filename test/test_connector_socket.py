"""The connector socket: hello and tokens, receipts and dedup, replies, sends, limits."""

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "container" / "lib"))

import connector_broker as broker_mod  # noqa: E402
import connector_client as client_mod  # noqa: E402
import connector_protocol as wire  # noqa: E402
from connector_contract import Ack, Choice, Reply  # noqa: E402

ROUTE = {"chat_id": "42"}


@pytest.fixture
def run_dir():
    # Unix socket paths are short (104 bytes on macOS): not under pytest's tmp_path.
    path = Path(tempfile.mkdtemp(prefix="wsc", dir="/tmp"))
    yield path / "run"
    shutil.rmtree(path, ignore_errors=True)


def _broker(run_dir, handler, seen=None):
    seen = set() if seen is None else seen

    def dedup(name, event_id):
        key = (name, event_id)
        if key in seen:
            return False
        seen.add(key)
        return True

    broker = broker_mod.Broker(run_dir, dedup)
    broker.add("echo", handler, [("wolt", "pick a wolt")])
    return broker


def _client(run_dir, token, **handlers):
    return client_mod.Client("echo", socket_path=str(broker_mod.socket_path(run_dir, "echo")),
                             token=token, **handlers)


async def _echo(event, out):
    body = event["body"]
    if event["kind"] == "selection":
        await out.ack(Ack("done", alert=False))
        await out.edit(Reply("picked " + body["action"]))
        return
    await out.say(Reply("you said " + body.get("text", ""), ((Choice("again", "again"),),),
                        markdown=True))


def test_hello_welcome_event_receipt_and_say(run_dir):
    async def main():
        broker = _broker(run_dir, _echo)
        await broker.start()
        said = []

        async def on_say(frame):
            said.append(wire.reply_from_wire(frame["reply"]))
        client = _client(run_dir, broker_mod.new_token(run_dir, "echo"), on_say=on_say)
        welcome = await client.connect()
        assert welcome["commands"] == [["wolt", "pick a wolt"]]
        assert await client.event("message", ROUTE, "1", {"text": "hi"}, "e1") == "accepted"
        await broker.drain("echo")
        await client.ping()
        assert said == [Reply("you said hi", ((Choice("again", "again"),),), markdown=True)]
        # The same id again is a duplicate and is not dispatched twice.
        assert await client.event("message", ROUTE, "1", {"text": "hi"}, "e1") == "duplicate"
        await broker.drain("echo")
        await client.ping()
        assert len(said) == 1
        await client.close()
        await broker.stop()
    asyncio.run(main())


def test_selection_gets_ack_and_edit(run_dir):
    async def main():
        broker = _broker(run_dir, _echo)
        await broker.start()
        got = []

        async def on_ack(frame):
            got.append(("ack", wire.ack_from_wire(frame["ack"])))

        async def on_edit(frame):
            got.append(("edit", wire.reply_from_wire(frame["reply"]).text))
        client = _client(run_dir, broker_mod.new_token(run_dir, "echo"), on_ack=on_ack, on_edit=on_edit)
        await client.connect()
        await client.event("selection", ROUTE, "1", {"action": "wolt:pal"}, "e2")
        await broker.drain("echo")
        await client.ping()
        assert got == [("ack", Ack("done", False)), ("edit", "picked wolt:pal")]
        await client.close()
        await broker.stop()
    asyncio.run(main())


def test_a_stale_or_wrong_token_is_refused(run_dir):
    async def main():
        broker = _broker(run_dir, _echo)
        await broker.start()
        old = broker_mod.new_token(run_dir, "echo")
        broker_mod.new_token(run_dir, "echo")  # a newer launch
        with pytest.raises(client_mod.Refused):
            await _client(run_dir, old).connect()
        with pytest.raises(client_mod.Refused):
            await _client(run_dir, "nope").connect()
        await broker.stop()
    asyncio.run(main())


def test_socket_and_token_are_private(run_dir):
    async def main():
        broker = _broker(run_dir, _echo)
        await broker.start()
        broker_mod.new_token(run_dir, "echo")
        assert run_dir.stat().st_mode & 0o777 == 0o700
        assert broker_mod.socket_path(run_dir, "echo").stat().st_mode & 0o777 == 0o600
        assert broker_mod.token_path(run_dir, "echo").stat().st_mode & 0o777 == 0o600
        await broker.stop()
    asyncio.run(main())


def test_a_newer_connection_replaces_the_old(run_dir):
    async def main():
        broker = _broker(run_dir, _echo)
        await broker.start()
        token = broker_mod.new_token(run_dir, "echo")
        first = _client(run_dir, token)
        await first.connect()
        second = _client(run_dir, token)
        await second.connect()
        with pytest.raises(client_mod.Refused):
            await first._read_task
        assert broker.connected("echo")
        await second.close()
        await broker.stop()
    asyncio.run(main())


def test_unreceipted_events_are_resent_after_the_lodge_restarts(run_dir):
    async def main():
        seen = set()
        dispatched = []

        async def record(event, out):
            dispatched.append(event["id"])

        broker = _broker(run_dir, record, seen)
        await broker.start()
        token = broker_mod.new_token(run_dir, "echo")
        client = _client(run_dir, token)
        await client.connect()
        assert await client.event("message", ROUTE, "1", {"text": "a"}, "e1") == "accepted"
        await broker.stop()
        # The lodge is down: the event waits, unreceipted.
        assert await client.event("message", ROUTE, "1", {"text": "b"}, "e2", timeout=0.2) == "pending"
        broker = _broker(run_dir, record, seen)
        await broker.start()
        await client.connect()
        await client.ping()
        await broker.drain("echo")
        assert dispatched == ["e1", "e2"]
        assert not client._unreceipted
        await client.close()
        await broker.stop()
    asyncio.run(main())


def test_send_round_trip_and_failures(run_dir):
    async def main():
        broker = _broker(run_dir, _echo)
        await broker.start()
        sent = []

        async def on_send(frame):
            sent.append((frame["method"], frame["route"], frame["text"]))
            if frame["text"] == "refuse me":
                return {"status": "refused", "error": "Bad Request: no"}
            return {"status": "delivered", "message_id": "9"}
        client = _client(run_dir, broker_mod.new_token(run_dir, "echo"), on_send=on_send)
        await client.connect()
        result = await broker.send("echo", ROUTE, "message", "hello")
        assert result["status"] == "delivered" and result["message_id"] == "9"
        with pytest.raises(broker_mod.SendFailed) as failed:
            await broker.send("echo", ROUTE, "message", "refuse me")
        assert failed.value.status == "refused"
        assert sent == [("message", ROUTE, "hello"), ("message", ROUTE, "refuse me")]
        await client.close()
        await asyncio.sleep(0.05)
        with pytest.raises(broker_mod.ConnectorUnavailable):
            await broker.send("echo", ROUTE, "message", "nobody home")
        await broker.stop()
    asyncio.run(main())


def test_a_bad_frame_closes_the_connection(run_dir):
    async def main():
        broker = _broker(run_dir, _echo)
        await broker.start()
        client = _client(run_dir, broker_mod.new_token(run_dir, "echo"))
        await client.connect()
        client._writer.write(b'{"type":"event","id":"x y"}\n')
        await client._writer.drain()
        await asyncio.wait_for(client._read_task, 2)
        await asyncio.sleep(0.05)
        assert not broker.connected("echo")
        await broker.stop()
    asyncio.run(main())


def test_a_full_queue_answers_failed(run_dir, monkeypatch):
    monkeypatch.setattr(broker_mod, "QUEUE_LIMIT", 1)

    async def main():
        gate = asyncio.Event()

        async def slow(event, out):
            while not gate.is_set():
                await asyncio.sleep(0.01)

        broker = _broker(run_dir, slow)
        await broker.start()
        client = _client(run_dir, broker_mod.new_token(run_dir, "echo"))
        await client.connect()
        assert await client.event("message", ROUTE, "1", {"text": "a"}, "e1") == "accepted"
        assert await client.event("message", ROUTE, "1", {"text": "b"}, "e2") == "failed"
        gate.set()
        await client.close()
        await broker.stop()
    asyncio.run(main())


@pytest.mark.parametrize("frame", [
    {"type": "event", "id": "e", "kind": "message", "route": {"chat_id": "1"}, "sender": "1",
     "body": {"text": "x" * (wire.MAX_TEXT + 1)}},
    {"type": "event", "id": "e", "kind": "teleport", "route": {"chat_id": "1"}, "sender": "1", "body": {}},
    {"type": "event", "id": "e", "kind": "message", "route": {"Chat": "1"}, "sender": "1",
     "body": {"text": "x"}},
    {"type": "event", "id": "e", "kind": "attachment", "route": {"chat_id": "1"}, "sender": "1",
     "body": {"media": "image", "path": "/x", "name": "x", "mime": "image/png", "size": True}},
    {"type": "hello", "v": 2, "name": "echo", "token": "t"},
])
def test_frame_checks_refuse(frame):
    with pytest.raises(wire.FrameError):
        wire.decode(wire.encode(frame) if len(str(frame)) < wire.MAX_FRAME_BYTES else b"",
                    wire.FROM_CONNECTOR)


def test_an_oversized_frame_is_refused():
    with pytest.raises(wire.FrameError):
        wire.encode({"type": "say", "reply": {"text": "x" * wire.MAX_FRAME_BYTES}})
