"""The Telegram connector against a fake Bot API (MockTransport) and the real
lodge connector API. No bot token, no api.telegram.org: every call is the fake's."""

import asyncio
import json
import logging
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container" / "lib"))
sys.path.insert(0, str(ROOT / "connectors" / "telegram"))

import server.app as server_app  # noqa: E402
from server import connector_api  # noqa: E402
from server.connectors import AcceptedEvent, ConnectorStore, Limits  # noqa: E402
from woltspace_telegram import LodgeRefused, TelegramConnector, split_text  # noqa: E402

BOT_TOKEN = "123456:FAKE-not-a-real-token"
# The connector addresses the Bot API by its real host, so the fake transport
# must answer for that host. It is the only place the name appears: every
# client here is built on httpx.MockTransport, so no request can leave.
BOT_API = "https://" + "api.telegram.org"
LIMITS = Limits(outbound_file_max_bytes=1_000_000, upload_max_bytes=100_000, lease_seconds=60)


class FakeBotApi:
    """Enough of the Bot API for the connector: updates in, sends out, files served."""

    def __init__(self):
        self.updates: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self.sent: list[dict] = []
        self.files: dict[str, bytes] = {}
        self.fail: dict[str, dict] = {}     # method -> error body for the next call
        self.file_status: dict[str, int] = {}   # file name -> HTTP status for the next download
        self.next_message_id = 100
        self.polled_offsets: list[int | None] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        assert request.url.host == BOT_API.removeprefix("https://")
        if path.startswith(f"/file/bot{BOT_TOKEN}/"):
            name = path.split("/")[-1]
            if name in self.file_status:
                return httpx.Response(self.file_status.pop(name), content=b"")
            if name in self.files:
                return httpx.Response(200, content=self.files[name])
            return httpx.Response(404, json={"ok": False})
        assert path.startswith(f"/bot{BOT_TOKEN}/"), path
        method = path.rsplit("/", 1)[-1]
        if request.headers.get("content-type", "").startswith("application/json"):
            params = json.loads(request.content)
        else:
            params = {"_multipart": request.headers.get("content-type", ""), "_size": len(request.content)}
            raw = request.content
            for field in ("chat_id", "message_thread_id"):
                marker = f'name="{field}"\r\n\r\n'.encode()
                if marker in raw:
                    params[field] = raw.split(marker, 1)[1].split(b"\r\n", 1)[0].decode()
            for field in ("voice", "audio", "document"):
                if f'name="{field}"'.encode() in raw:
                    params["_field"] = field
        self.calls.append((method, params))
        if method in self.fail:
            body = self.fail.pop(method)
            return httpx.Response(200, json={"ok": False, **body})
        if method == "getUpdates":
            self.polled_offsets.append(params.get("offset"))
            offset = params.get("offset")
            if offset is not None:
                # Telegram confirms on offset: everything below it is gone for good.
                self.updates = [u for u in self.updates if u["update_id"] >= offset]
            return httpx.Response(200, json={"ok": True, "result": list(self.updates)})
        if method == "getFile":
            file_id = params["file_id"]
            return httpx.Response(200, json={"ok": True, "result": {"file_id": file_id, "file_path": f"files/{file_id}"}})
        if method in ("sendMessage", "sendDocument", "sendVoice", "sendAudio"):
            self.next_message_id += 1
            record = {"method": method, "message_id": self.next_message_id, **params}
            self.sent.append(record)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": self.next_message_id}})
        return httpx.Response(200, json={"ok": False, "description": f"unknown method {method}"})


def _update(update_id, text=None, *, chat=101, user=101, reply_to=None, **message):
    body = {"message_id": update_id * 10, "from": {"id": user, "username": "jer"}, "chat": {"id": chat}, **message}
    if text is not None:
        body["text"] = text
    if reply_to is not None:
        body["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": update_id, "message": body}


class Lodge:
    def __init__(self, tmp_path):
        self.store = ConnectorStore(tmp_path / "connectors", limits=LIMITS)
        self.routed: list[AcceptedEvent] = []
        self.store.router = lambda event: self.routed.append(event) or {"routed": "test"}
        self.store.add_instance("tg", "telegram", "bot:123456")
        self.binding = self.store.bind("tg", sender="101", conversation="101", default_wolt="softie")
        self.store.enable("tg")
        self.token = self.store.issue_token("tg")

    def enqueue(self, text="hello", **kwargs) -> str:
        return self.store.enqueue(
            channel="telegram", account=None, conversation="101", text=text, session="softie-1",
            reply_footer="\n\n---\n↩️ reply to this message\nhttps://lodge/tui?session=softie-1", **kwargs,
        )

    def deliveries(self):
        return {d["delivery_id"]: d for d in self.store.list_deliveries("tg")}


@pytest.fixture
def lodge(tmp_path, monkeypatch):
    lodge = Lodge(tmp_path)
    monkeypatch.setattr(connector_api, "CONNECTORS_DIR", lodge.store.root)
    monkeypatch.setattr(connector_api, "_store", lodge.store)
    monkeypatch.setattr(connector_api, "_POLL_INTERVAL", 0.01)
    return lodge


@pytest.fixture
def api():
    return FakeBotApi()


def _connector(lodge, api, token=None) -> TelegramConnector:
    lodge_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server_app.app, client=("127.0.0.1", 1)),
        base_url="http://localhost:7777",
    )
    bot = httpx.AsyncClient(transport=api.transport(), base_url=BOT_API)
    return TelegramConnector(lodge_client, bot, bot_token=BOT_TOKEN, connector_token=token or lodge.token)


# ---------------------------------------------------------------- inbound


async def test_text_updates_become_accepted_events_and_the_cursor_follows(lodge, api):
    api.updates = [_update(41, "hello"), _update(42, "/start"), _update(43, "from a stranger", user=202)]
    connector = _connector(lodge, api)
    state = await connector.start()
    assert state["cursor"] is None
    report = await connector.poll_once(timeout=0)
    # One fetch, then the confirming ask with the new offset.
    assert api.polled_offsets == [None, 44]
    assert [e["body"].get("accepted") for e in report.events] == [True, True, False]
    assert report.events[2]["body"]["reason"] == "sender_not_allowed"
    assert report.cursor == 44
    # Commands are passed through as text in this slice.
    assert [e.text for e in lodge.routed] == ["hello", "/start"]
    assert lodge.routed[0].event_id == "tg:41" and lodge.routed[0].cursor == 41
    assert lodge.routed[0].sender == {"provider_id": "101", "display": "jer"}
    # The next poll asks from the cursor, and a restart takes it from core.
    await connector.poll_once(timeout=0)
    assert api.polled_offsets[-1] == 44
    connector.stop()
    fresh = _connector(lodge, api)
    assert (await fresh.start())["cursor"] == 44
    assert len(lodge.routed) == 2


async def test_the_cursor_never_moves_past_an_update_the_lodge_did_not_answer(lodge, api, monkeypatch):
    api.updates = [_update(41, "one"), _update(42, "two"), _update(43, "three")]
    connector = _connector(lodge, api)
    original = lodge.store.accept_event
    calls = []

    def flaky(who, payload):
        calls.append(payload["event_id"])
        if payload["event_id"] == "tg:42" and len(calls) < 3:
            from server.connectors import ConnectorStateUnreadable
            raise ConnectorStateUnreadable("disk hiccup")
        return original(who, payload)

    monkeypatch.setattr(lodge.store, "accept_event", flaky)
    report = await connector.poll_once(timeout=0)
    assert [e["status"] for e in report.events] == [200, 503]
    assert report.cursor == 42
    assert [e.event_id for e in lodge.routed] == ["tg:41"]
    # The next poll re-fetches from 42; the lodge answers it and 43, and 41 is
    # not asked for again.
    report = await connector.poll_once(timeout=0)
    # Fetched from 42 (41 confirmed by the earlier short ask), then confirmed at 44.
    assert api.polled_offsets == [None, 42, 42, 44]
    assert [e["status"] for e in report.events] == [200, 200]
    assert report.cursor == 44
    assert [e.event_id for e in lodge.routed] == ["tg:41", "tg:42", "tg:43"]
    # A re-poll of an already answered update is answered from dedup, once more, no new handoff.
    api.updates.append(_update(41, "one"))
    await connector.poll_once(timeout=0)
    assert len(lodge.routed) == 3


async def test_media_is_downloaded_uploaded_and_named_in_the_event(lodge, api):
    api.files["v1"] = b"OggS voice"
    api.files["d1"] = b"%PDF-1.4"
    api.updates = [
        _update(51, voice={"file_id": "v1", "file_unique_id": "uv1"}, caption="listen"),
        _update(52, document={"file_id": "d1", "file_unique_id": "ud1", "file_name": "../../report.pdf"}),
        _update(53, photo=[{"file_id": "p1", "file_unique_id": "up1", "file_size": 10}, {"file_id": "p2", "file_unique_id": "up2", "file_size": 99}]),
    ]
    api.files["p2"] = b"\xff\xd8 big photo"
    connector = _connector(lodge, api)
    report = await connector.poll_once(timeout=0)
    assert [e["body"]["accepted"] for e in report.events] == [True, True, True]
    voice, document, photo = lodge.routed
    assert voice.text == "listen" and voice.attachments[0]["name"] == "voice_uv1.ogg"
    assert Path(voice.attachments[0]["path"]).read_bytes() == b"OggS voice"
    assert document.attachments[0]["name"] == "report.pdf"
    assert photo.attachments[0]["name"] == "photo_up2.jpg"
    assert Path(photo.attachments[0]["path"]).read_bytes() == b"\xff\xd8 big photo"
    # The largest photo size was fetched, nothing else.
    assert [p["file_id"] for m, p in api.calls if m == "getFile"] == ["v1", "d1", "p2"]


async def test_a_file_the_api_cannot_serve_still_delivers_the_text(lodge, api):
    api.updates = [_update(61, document={"file_id": "missing", "file_unique_id": "um"}, caption="see attached")]
    connector = _connector(lodge, api)
    report = await connector.poll_once(timeout=0)
    assert report.events[0]["body"]["accepted"] is True
    assert lodge.routed[0].attachments == ()
    assert "could not be carried: file download answered 404" in lodge.routed[0].text


async def test_replies_to_the_bots_messages_route_through_the_message_map(lodge, api):
    delivery_id = lodge.enqueue("a long answer " * 400)
    connector = _connector(lodge, api)
    report = await connector.deliver_once()
    ack = report.acks[0]["body"]
    assert ack["status"] == "delivered"
    parts = [m for m in api.sent if m["method"] == "sendMessage"]
    assert len(parts) == 2 and all(len(m["text"]) <= 4096 for m in parts)
    assert parts[-1]["text"].endswith("https://lodge/tui?session=softie-1")
    ids = lodge.deliveries()[delivery_id]["provider_message_ids"]
    assert ids == [str(m["message_id"]) for m in parts]
    # A reply to the first part (no footer on it) still finds the session.
    api.updates = [_update(71, "re: part one", reply_to=parts[0]["message_id"]),
                   _update(72, "re: nothing", reply_to=424242)]
    report = await connector.poll_once(timeout=0)
    assert report.events[0]["body"]["session"] == "softie-1"
    assert report.events[1]["body"]["reason"] == "reply_to_out_of_binding"
    assert lodge.routed[0].reply_to_session == "softie-1"


# ---------------------------------------------------------------- outbound


def test_split_text_cuts_at_line_ends_within_the_limit():
    text = "\n".join(f"line {i:04d}" for i in range(1000))
    parts = split_text(text)
    assert all(len(p) <= 4096 for p in parts)
    assert "".join(p.replace("\n", "") for p in parts) == text.replace("\n", "")
    assert all(not p.startswith("\n") for p in parts)
    assert split_text("") == [""]
    assert split_text("x" * 5000) == ["x" * 4096, "x" * 904]


async def test_attachments_go_as_voice_audio_or_document_with_document_fallback(lodge, api, tmp_path):
    for name, kind in (("note.ogg", "voice"), ("song.mp3", "file"), ("report.pdf", "file"), ("talk.ogg", "voice")):
        path = tmp_path / name
        path.write_bytes(b"bytes of " + name.encode())
        lodge.enqueue(f"here is {name}", attachment=path, attachment_kind=kind)
    api.fail["sendVoice"] = {"error_code": 400, "description": "Bad Request: VOICE_MESSAGES_FORBIDDEN"}
    connector = _connector(lodge, api)
    # The forbidden voice is the first delivery leased; it falls back to a document.
    report = await connector.deliver_once()
    assert [a["body"]["status"] for a in report.acks] == ["delivered"] * 4
    methods = [m["method"] for m in api.sent if m["method"] != "sendMessage"]
    assert methods == ["sendDocument", "sendAudio", "sendDocument", "sendVoice"]
    assert [m["_field"] for m in api.sent if m["method"] != "sendMessage"] == ["document", "audio", "document", "voice"]
    for delivery in lodge.deliveries().values():
        assert len(delivery["provider_message_ids"]) == 2


async def test_telegram_errors_are_nacked_retryable_or_terminal(lodge, api):
    busy = lodge.enqueue("busy")
    api.fail["sendMessage"] = {"error_code": 429, "description": "Too Many Requests: retry after 3"}
    connector = _connector(lodge, api)
    report = await connector.deliver_once()
    assert report.acks[0]["body"]["status"] == "queued"
    gone = lodge.enqueue("gone")
    api.fail["sendMessage"] = {"error_code": 403, "description": "Forbidden: bot was blocked by the user"}
    report = await connector.deliver_once()
    assert report.acks[0]["body"]["status"] == "failed"
    records = lodge.deliveries()
    assert records[busy]["failed_attempts"] == 1 and records[busy]["status"] == "queued"
    assert records[gone]["status"] == "failed" and "blocked" in records[gone]["error"]


async def test_a_partial_send_is_a_failed_attempt_and_may_repeat_the_sent_parts(lodge, api):
    delivery_id = lodge.enqueue("x" * 5000)
    connector = _connector(lodge, api)
    calls = []
    real = api.handle

    def second_part_times_out(request):
        if request.url.path.endswith("/sendMessage"):
            calls.append(1)
            if len(calls) == 2:
                raise httpx.ReadTimeout("timed out", request=request)
        return real(request)

    connector.bot = httpx.AsyncClient(transport=httpx.MockTransport(second_part_times_out), base_url=BOT_API)
    report = await connector.deliver_once()
    assert report.acks[0]["body"]["status"] == "queued"
    sent_first = [m for m in api.sent if m["method"] == "sendMessage"]
    assert len(sent_first) == 1
    # The part that went out is mapped even though the attempt failed.
    assert lodge.deliveries()[delivery_id]["provider_message_ids"] == [str(sent_first[0]["message_id"])]
    # Documented duplicate: the retry resends part one.
    lodge.store.clock = lambda: __import__("time").time() + 10
    report = await connector.deliver_once()
    assert report.acks[0]["body"]["status"] == "delivered"
    assert len([m for m in api.sent if m["method"] == "sendMessage"]) == 3
    assert len(lodge.deliveries()[delivery_id]["provider_message_ids"]) == 3


async def test_a_paused_or_revoked_lodge_stops_the_connector(lodge, api):
    connector = _connector(lodge, api)
    lodge.store.disable("tg")
    assert (await connector.deliver_once()).paused is True
    lodge.store.revoke_tokens("tg")
    with pytest.raises(LodgeRefused):
        await connector.deliver_once()
    api.updates = [_update(91, "anyone there?")]
    with pytest.raises(LodgeRefused):
        await connector.poll_once(timeout=0)
    # The fence comes before getUpdates: nothing was polled, nothing confirmed.
    assert api.polled_offsets == []
    assert [u["update_id"] for u in api.updates] == [91]


async def test_the_bot_token_never_reaches_a_log_line(lodge, api, caplog):
    api.updates = [_update(81, "hi")]
    connector = _connector(lodge, api)

    def explode(request):
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    connector.bot = httpx.AsyncClient(transport=httpx.MockTransport(explode), base_url=BOT_API)
    with caplog.at_level(logging.WARNING):
        await connector.poll_once(timeout=0)
    assert caplog.records and BOT_TOKEN not in caplog.text
    assert "<token>" in caplog.text
    lodge.enqueue("hello")
    with caplog.at_level(logging.WARNING):
        report = await connector.deliver_once()
    assert BOT_TOKEN not in json.dumps(report.acks)
    assert BOT_TOKEN not in json.dumps(lodge.deliveries())


# ------------------------------------------------- round 2: fence, media, logging


def _switched_lodge(tmp_path, monkeypatch, runtime="connector"):
    """A lodge whose store reads the switch from a woltspace.json of its own."""
    from server.connectors import set_channel_runtime

    config = tmp_path / "woltspace.json"
    lodge = Lodge(tmp_path)
    lodge.store.runtime_config = config
    set_channel_runtime(config, "telegram", runtime)
    monkeypatch.setattr(connector_api, "CONNECTORS_DIR", lodge.store.root)
    monkeypatch.setattr(connector_api, "_store", lodge.store)
    monkeypatch.setattr(connector_api, "_POLL_INTERVAL", 0.01)
    return lodge, config


async def test_the_fence_stops_polling_and_sending_when_ownership_is_lost_and_resumes_after(tmp_path, monkeypatch):
    """Finding 1, both directions, with a Telegram that confirms on offset."""
    from server.connectors import set_channel_runtime

    lodge, config = _switched_lodge(tmp_path, monkeypatch)
    api = FakeBotApi()
    api.updates = [_update(41, "one"), _update(42, "two")]
    connector = _connector(lodge, api)
    report = await connector.poll_once(timeout=0)
    assert report.cursor == 43 and [e.event_id for e in lodge.routed] == ["tg:41", "tg:42"]
    # connector -> adapter: the file says adapter. The next poll is fenced
    # before getUpdates, so Telegram keeps 43 unconfirmed for the adapter.
    api.updates.append(_update(43, "three"))
    set_channel_runtime(config, "telegram", "adapter")
    report = await connector.poll_once(timeout=0)
    assert report.fenced is True and report.events == []
    # 41 and 42 were confirmed right after the lodge answered them; 43 was
    # never asked for, so Telegram still holds it for the adapter.
    assert api.polled_offsets[-1] == 43 and len(api.polled_offsets) == 2
    assert [u["update_id"] for u in api.updates] == [43]
    # A send already leased is fenced too: nothing goes to Telegram, no ack.
    set_channel_runtime(config, "telegram", "connector")
    delivery_id = lodge.enqueue("hello")
    leased = await connector.lease_batch()
    assert [d["delivery_id"] for d in leased["deliveries"]] == [delivery_id]
    set_channel_runtime(config, "telegram", "adapter")
    from woltspace_telegram import Report
    report = await connector.send_leased(leased["deliveries"], Report())
    assert report.fenced is True and report.acks == [] and api.sent == []
    assert lodge.deliveries()[delivery_id]["status"] == "leased"
    # A held long poll loses too: it ends paused with nothing.
    assert (await connector.lease_batch(wait=0.2)) == {"deliveries": [], "paused": True}
    # adapter -> connector: the backlog Telegram kept is what the connector gets.
    set_channel_runtime(config, "telegram", "connector")
    lodge.store.clock = lambda: __import__("time").time() + 120   # the lease expired
    report = await connector.poll_once(timeout=0)
    assert [e["body"]["event_id"] for e in report.events] == ["tg:43"]
    assert report.cursor == 44
    report = await connector.deliver_once()
    assert report.acks[0]["body"]["status"] == "delivered"


async def test_transient_media_failures_hold_the_cursor_and_permanent_ones_become_a_note(lodge, api):
    api.updates = [
        _update(51, document={"file_id": "d1", "file_unique_id": "ud1", "file_name": "a.pdf"}),
        _update(52, "after"),
    ]
    api.files["d1"] = b"%PDF"
    connector = _connector(lodge, api)
    # getFile 429: the batch stops at 51, nothing after it is answered.
    api.fail["getFile"] = {"error_code": 429, "description": "Too Many Requests"}
    report = await connector.poll_once(timeout=0)
    assert (report.events, report.stopped_at, report.cursor) == ([], 51, None)
    assert lodge.routed == []
    # Download 503: same.
    api.file_status["d1"] = 503
    report = await connector.poll_once(timeout=0)
    assert (report.events, report.stopped_at) == ([], 51)
    # Lodge upload 429 (quota): same, and the update is still unconfirmed.
    from server.connectors import Limits as _Limits
    lodge.store.limits = _Limits(outbound_file_max_bytes=1_000_000, upload_disk_cap_bytes=1)
    report = await connector.poll_once(timeout=0)
    assert (report.events, report.stopped_at) == ([], 51)
    assert [u["update_id"] for u in api.updates] == [51, 52]
    # Pressure gone: both are carried, in order, with the file.
    lodge.store.limits = LIMITS
    report = await connector.poll_once(timeout=0)
    assert [e["body"]["event_id"] for e in report.events] == ["tg:51", "tg:52"]
    assert lodge.routed[0].attachments[0]["name"] == "a.pdf"
    assert report.cursor == 53
    # Permanent (404): a note, the event goes on.
    api.updates = [_update(53, document={"file_id": "gone", "file_unique_id": "ug"})]
    report = await connector.poll_once(timeout=0)
    assert report.events[0]["body"]["accepted"] is True
    assert "could not be carried" in lodge.routed[-1].text and report.cursor == 54


async def test_an_oversized_download_is_cut_while_streaming(lodge, api, monkeypatch):
    import woltspace_telegram as module

    monkeypatch.setattr(module, "DOWNLOAD_CAP", 100)
    served = []

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(50):
                served.append(1)
                yield b"x" * 10

    def handle(request):
        if request.url.path.startswith(f"/file/bot{BOT_TOKEN}/"):
            return httpx.Response(200, stream=Chunks())
        return api.handle(request)

    api.updates = [_update(61, document={"file_id": "big", "file_unique_id": "ub", "file_name": "big.bin"}, caption="see")]
    connector = _connector(lodge, api)
    connector.bot = httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url=BOT_API)
    report = await connector.poll_once(timeout=0)
    assert report.events[0]["body"]["accepted"] is True
    assert "over 100 bytes" in lodge.routed[0].text and lodge.routed[0].attachments == ()
    assert len(served) < 50


async def test_unsupported_updates_are_ignored_durably_and_the_frontier_follows_fetch_order(lodge, api, monkeypatch):
    """Finding 6: ignores, gaps, an unanswered earlier item, a replay with another cursor."""
    api.updates = [
        _update(41, "one"),
        {"update_id": 42, "my_chat_member": {"chat": {"id": 101}}},        # a service update
        _update(50, "after a gap"),
    ]
    connector = _connector(lodge, api)
    report = await connector.poll_once(timeout=0)
    assert [(e["body"].get("accepted"), e["body"].get("reason")) for e in report.events] == [
        (True, None), (False, "ignored_unsupported_update"), (True, None),
    ]
    assert report.cursor == 51
    assert api.updates == []
    # An unanswered earlier item: the lodge cannot take 61 (503), 62 is fine.
    api.updates = [_update(61, "stuck"), _update(62, "later")]
    original = lodge.store.accept_event
    calls = []

    def flaky(who, payload):
        calls.append(payload["event_id"])
        if payload["event_id"] == "tg:61" and len(calls) == 1:
            from server.connectors import ConnectorStateUnreadable
            raise ConnectorStateUnreadable("hiccup")
        return original(who, payload)

    monkeypatch.setattr(lodge.store, "accept_event", flaky)
    report = await connector.poll_once(timeout=0)
    assert (report.stopped_at, report.cursor, calls) == (61, 51, ["tg:61"])
    assert [u["update_id"] for u in api.updates] == [61, 62]
    report = await connector.poll_once(timeout=0)
    assert (report.cursor, calls) == (63, ["tg:61", "tg:61", "tg:62"])
    # The same text at a new position is a different event; the recorded
    # position of an old one cannot be relabelled (the store-level test
    # `test_a_replay_with_another_cursor_is_a_conflict_not_a_relabel` covers
    # the conflict itself: here the connector's cursor comes from core).
    monkeypatch.setattr(lodge.store, "accept_event", original)
    api.updates = [_update(99, "one")]
    report = await connector.poll_once(timeout=0)
    assert report.cursor == 100
    assert (await connector._lodge("GET", "/state")).json()["cursor"] == 100


async def test_the_runners_logging_never_shows_the_token_even_at_debug(lodge, api, caplog, tmp_path, monkeypatch):
    """Finding 4: successful requests, downloads, sends and failures, at DEBUG."""
    import woltspace_telegram.runner as runner

    runner.configure_logging(BOT_TOKEN, level=logging.DEBUG)
    try:
        api.files["d1"] = b"%PDF"
        api.updates = [_update(41, document={"file_id": "d1", "file_unique_id": "ud1", "file_name": "a.pdf"})]
        api.fail["sendMessage"] = {"error_code": 400, "description": f"Bad Request: see https://x/bot{BOT_TOKEN}/sendMessage"}
        lodge.enqueue("hello")
        connector = _connector(lodge, api)
        with caplog.at_level(logging.DEBUG):
            await connector.poll_once(timeout=0)
            report = await connector.deliver_once()
            logging.getLogger("httpx").info("HTTP Request: POST https://host/bot%s/getUpdates", BOT_TOKEN)
            logging.getLogger("woltspace_telegram").warning("failed: %s", f"url=/bot{BOT_TOKEN}/x")
            try:
                raise RuntimeError(f"boom /bot{BOT_TOKEN}/")
            except RuntimeError:
                logging.getLogger("woltspace_telegram").exception("with traceback")
        assert BOT_TOKEN not in caplog.text
        assert "<token>" in caplog.text
        assert not [r for r in caplog.records if r.name.startswith("httpx") and r.levelno < logging.WARNING]
        # Provider descriptions and nack errors are redacted too.
        error = lodge.deliveries()[report.acks[0]["delivery_id"]]["error"]
        assert BOT_TOKEN not in error and "<token>" in error
        assert BOT_TOKEN not in json.dumps(report.acks)
    finally:
        logging.setLogRecordFactory(logging.LogRecord)
        logging.getLogger().setLevel(logging.WARNING)


# ------------------------------------------------- round 3: per-call fences, run lock


async def test_the_fence_sits_before_every_provider_call_not_every_delivery(tmp_path):
    """Astre's probe: ownership withdrawn after part one; and _confirm while not owning."""
    from woltspace_telegram import Report

    active = [True]
    calls = []
    acks = []

    def lodge(request):
        if request.url.path.endswith("/state"):
            return httpx.Response(200, json={
                "runtime_active": active[0], "paused": False, "cursor": 8, "run_lock": str(tmp_path / "run.lock"),
            })
        if request.url.path.endswith("/ack"):
            acks.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    def bot(request):
        calls.append(request.url.path.rsplit("/", 1)[-1])
        active[0] = False
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(calls)}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(lodge), base_url="http://fixture.invalid") as lc, httpx.AsyncClient(transport=httpx.MockTransport(bot), base_url=BOT_API) as bc:
        connector = TelegramConnector(lc, bc, bot_token="FAKE", connector_token="FAKE")
        await connector.start()
        delivery = {"delivery_id": "d", "attempt_id": "a", "conversation": {"provider_id": "chat"}, "text": "x" * 5000}
        report = await connector.send_leased([delivery], Report())
        assert calls == ["sendMessage"]
        assert report.fenced is True
        assert acks == [{"attempt_id": "a", "ok": False, "retry": True, "error": "not sending sendMessage: the channel is no longer ours", "provider_message_ids": ["1"]}]
        calls.clear()
        connector.cursor = 9
        await connector._confirm()
        assert calls == []
        # A file send's fallback is fenced too: ownership back, lost after the first file call.
        active[0] = True
        acks.clear()

        def bot_refusing_voice(request):
            calls.append(request.url.path.rsplit("/", 1)[-1])
            active[0] = False
            return httpx.Response(200, json={"ok": False, "error_code": 400, "description": "VOICE_MESSAGES_FORBIDDEN"})

        def lodge_with_file(request):
            if request.url.path.startswith("/connector/v1/files/"):
                return httpx.Response(200, content=b"opus")
            return lodge(request)

        connector.lodge = httpx.AsyncClient(transport=httpx.MockTransport(lodge_with_file), base_url="http://fixture.invalid")
        connector.bot = httpx.AsyncClient(transport=httpx.MockTransport(bot_refusing_voice), base_url=BOT_API)
        delivery = {"delivery_id": "d2", "attempt_id": "b", "conversation": {"provider_id": "chat"}, "text": "",
                    "attachments": [{"file_id": "f" * 32, "name": "n.ogg", "kind": "voice", "size": 4}]}
        await connector.send_leased([delivery], Report())
        assert calls == ["sendVoice"]
        assert acks[0]["ok"] is False and acks[0]["retry"] is True


async def test_the_run_lock_is_taken_before_the_first_provider_call_and_held_until_stop(lodge, api):
    import fcntl

    lock_path = lodge.store.root / "run-telegram.lock"
    held_at_call = []

    def is_held():
        with open(lock_path, "a+") as probe:
            try:
                fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
            return False

    real = api.handle

    def observing(request):
        held_at_call.append(is_held())
        return real(request)

    api.updates = [_update(41, "hi")]
    connector = _connector(lodge, api)
    connector.bot = httpx.AsyncClient(transport=httpx.MockTransport(observing), base_url=BOT_API)
    state = await connector.start()
    assert state["run_lock"] == str(lock_path) and is_held()
    lodge.enqueue("hello")
    await connector.poll_once(timeout=0)
    await connector.deliver_once()
    assert held_at_call and all(held_at_call)
    # A second connector for the channel cannot start, and neither loop runs without the lock.
    from woltspace_telegram import NotOwner
    second = _connector(lodge, api)
    with pytest.raises(NotOwner):
        await second.start()
    second.started, second.run_lock = True, str(lock_path)
    with pytest.raises(NotOwner):
        await second.poll_once(timeout=0)
    with pytest.raises(NotOwner):
        await second.deliver_once()
    assert len(held_at_call) == 3   # getUpdates, confirm, sendMessage; nothing from the second
    connector.stop()
    assert not is_held()


async def test_the_runner_holds_the_lock_through_both_loops_and_their_calls(lodge, api, monkeypatch):
    """`run()` ends only when both loops have ended; the lock is still held then, released after."""
    import fcntl
    import woltspace_telegram.runner as runner

    lock_path = lodge.store.root / "run-telegram.lock"

    def is_held():
        with open(lock_path, "a+") as probe:
            try:
                fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
            return False

    api.updates = [_update(41, "hi")]
    connector = _connector(lodge, api)
    turns = {"poll": 0, "deliver": 0}
    real_poll, real_deliver = connector.poll_once, connector.deliver_once

    async def poll_then_die(**kwargs):
        turns["poll"] += 1
        await real_poll(timeout=0)
        assert is_held()
        lodge.store.revoke_tokens("tg")
        return await real_poll(timeout=0)

    async def deliver_until_refused(**kwargs):
        turns["deliver"] += 1
        return await real_deliver()

    monkeypatch.setattr(connector, "poll_once", poll_then_die)
    monkeypatch.setattr(connector, "deliver_once", deliver_until_refused)
    assert await runner.run(connector, bot_token=BOT_TOKEN) == 1
    assert turns["poll"] >= 1 and turns["deliver"] >= 1
    assert is_held()
    connector.stop()
    assert not is_held()


# ------------------------------------------- round 4: shutdown and fail-closed start


async def test_run_cancels_and_awaits_the_sibling_loop_before_returning():
    """Astre's probe: inbound refused while outbound is held in a provider operation."""
    import woltspace_telegram.runner as runner
    from woltspace_telegram import LodgeRefused

    entered = asyncio.Event()
    completed = asyncio.Event()

    class Fake:
        async def start(self):
            return {}

        async def poll_once(self, **kwargs):
            await entered.wait()
            raise LodgeRefused("revoked")

        async def deliver_once(self, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                completed.set()

    result = await runner.run(Fake(), bot_token="FAKE")
    assert (result, completed.is_set()) == (1, True)
    assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and t.get_name() in ("inbound", "outbound")] == []


async def test_a_competing_run_lock_stays_refused_until_the_held_operation_cleans_up(lodge, api, monkeypatch):
    import fcntl
    import woltspace_telegram.runner as runner
    from woltspace_telegram import LodgeRefused

    lock_path = lodge.store.root / "run-telegram.lock"
    entered = asyncio.Event()
    completed = asyncio.Event()
    connector = _connector(lodge, api)

    async def poll_refused(**kwargs):
        await entered.wait()
        raise LodgeRefused("revoked")

    async def deliver_held(**kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()       # a provider operation that never answers
        finally:
            await asyncio.sleep(0.05)          # its cleanup takes a moment
            completed.set()

    monkeypatch.setattr(connector, "poll_once", poll_refused)
    monkeypatch.setattr(connector, "deliver_once", deliver_held)

    def competing_acquire() -> bool:
        with open(lock_path, "a+") as probe:
            try:
                fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
            return True

    run_task = asyncio.create_task(runner.run(connector, bot_token=BOT_TOKEN))
    await entered.wait()
    assert competing_acquire() is False          # the held operation is in flight
    assert await run_task == 1
    assert completed.is_set()                    # cleanup finished before run returned
    assert competing_acquire() is False          # and the lock is still held until stop()
    connector.stop()
    assert competing_acquire() is True


async def test_cancelling_the_run_cancels_both_loops_before_the_lock_goes(lodge, api, monkeypatch):
    import woltspace_telegram.runner as runner

    connector = _connector(lodge, api)
    finished = []

    async def forever(**kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            finished.append(1)

    monkeypatch.setattr(connector, "poll_once", forever)
    monkeypatch.setattr(connector, "deliver_once", forever)
    run_task = asyncio.create_task(runner.run(connector, bot_token=BOT_TOKEN))
    await asyncio.sleep(0.05)
    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task
    assert finished == [1, 1]
    assert connector.holds_run_lock()
    connector.stop()


@pytest.mark.parametrize("answer", [
    ("503", lambda tmp: httpx.Response(503, json={"error": "temporary"})),
    ("not json", lambda tmp: httpx.Response(200, content=b"<html>")),
    ("not an object", lambda tmp: httpx.Response(200, json=["x"])),
    ("no run lock", lambda tmp: httpx.Response(200, json={"runtime_active": True, "paused": False})),
    ("empty run lock", lambda tmp: httpx.Response(200, json={"runtime_active": True, "run_lock": ""})),
])
async def test_start_fails_closed_without_an_admitted_state_and_a_run_lock(tmp_path, answer):
    """Finding 2: a bad first state answer never becomes a started connector."""
    from woltspace_telegram import NotOwner, StartRefused

    label, first = answer
    calls = []
    n = [0]

    def lodge(request):
        n[0] += 1
        if n[0] == 1:
            return first(tmp_path)
        return httpx.Response(200, json={"runtime_active": True, "paused": False, "run_lock": str(tmp_path / "run.lock")})

    def bot(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"ok": True, "result": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(lodge), base_url="http://fixture.invalid") as lc, httpx.AsyncClient(transport=httpx.MockTransport(bot), base_url=BOT_API) as bc:
        connector = TelegramConnector(lc, bc, bot_token="FAKE", connector_token="FAKE")
        with pytest.raises(StartRefused):
            await connector.start()
        assert connector.started is False and connector.holds_run_lock() is False
        # A later good state answer does not repair it: nothing reaches the provider.
        with pytest.raises(NotOwner):
            await connector._call("getUpdates")
        with pytest.raises(NotOwner):
            await connector._confirm() if False else connector._require_lock()
        assert calls == []
        # Started properly, it works; stopped, it refuses again.
        await connector.start()
        assert connector.holds_run_lock()
        await connector._call("getUpdates")
        assert len(calls) == 1
        connector.stop()
        with pytest.raises(NotOwner):
            await connector._call("getUpdates")
        assert len(calls) == 1 and connector.started is False


async def test_provider_calls_before_start_and_after_stop_are_refused(lodge, api):
    from woltspace_telegram import NotOwner

    connector = _connector(lodge, api)
    with pytest.raises(NotOwner):
        await connector._call("getUpdates")
    with pytest.raises(NotOwner):
        await connector._upload_call("sendDocument", "document", b"x", "x", {"chat_id": "1"})
    assert api.calls == []
    await connector.start()
    connector.stop()
    with pytest.raises(NotOwner):
        await connector.poll_once(timeout=0)
    with pytest.raises(NotOwner):
        await connector.deliver_once()
    assert api.calls == []


# ------------------------------------------------- part B: buttons and button presses


async def test_buttons_render_as_an_inline_keyboard_and_presses_become_callback_events(lodge, api):
    lodge.store.bind("tg", sender="101", conversation="101", default_wolt="softie", selectable_wolts=["softie", "otter"])
    lodge.store.router = lambda event: lodge.routed.append(event) or {"routed": "test"}
    lodge.enqueue("pick one", buttons=[[{"text": "softie", "action": {"command": "wolt", "argument": "softie"}},
                                        {"text": "otter", "action": {"command": "wolt", "argument": "otter"}}]])
    connector = _connector(lodge, api)
    report = await connector.deliver_once()
    assert report.acks[0]["body"]["status"] == "delivered"
    [sent] = [m for m in api.sent if m["method"] == "sendMessage"]
    keyboard = sent["reply_markup"]["inline_keyboard"]
    assert [b["text"] for b in keyboard[0]] == ["softie", "otter"]
    assert all(b["callback_data"].startswith("cb_") for b in keyboard[0])
    # A press on the second button arrives as a callback_query and becomes a callback event.
    api.updates = [{
        "update_id": 71,
        "callback_query": {"id": "q1", "from": {"id": 101, "username": "jer"},
                           "message": {"message_id": sent["message_id"], "chat": {"id": 101}},
                           "data": keyboard[0][1]["callback_data"]},
    }]
    report = await connector.poll_once(timeout=0)
    assert report.events[0]["body"]["accepted"] is True and report.cursor == 72
    assert lodge.routed[0].callback == keyboard[0][1]["callback_data"] and lodge.routed[0].text == ""
    assert [m for m, p in api.calls if m == "answerCallbackQuery"] == ["answerCallbackQuery"]
    # A callback query without data is ignored durably.
    api.updates = [{"update_id": 72, "callback_query": {"id": "q2", "from": {"id": 101}, "message": {"chat": {"id": 101}}}}]
    report = await connector.poll_once(timeout=0)
    assert report.events[0]["body"]["reason"] == "ignored_unsupported_update" and report.cursor == 73


async def test_malformed_or_legacy_callback_data_is_disposed_of_once_and_later_updates_flow(lodge, api):
    lodge.store.router = lambda event: lodge.routed.append(event) or {"routed": "test"}
    api.updates = [
        {"update_id": 81, "callback_query": {"id": "q1", "from": {"id": 101}, "message": {"chat": {"id": 101}}, "data": "legacy:wolt:softie"}},
        {"update_id": 82, "callback_query": {"id": "q2", "from": {"id": 101}, "message": {"chat": {"id": 101}}, "data": "cb_" + "0" * 32}},
        _update(83, "after the buttons"),
    ]
    connector = _connector(lodge, api)
    report = await connector.poll_once(timeout=0)
    assert [(e["status"], e["body"].get("reason")) for e in report.events] == [
        (200, "ignored_unsupported_callback"), (200, None), (200, None),
    ]
    assert report.cursor == 84 and [e.text for e in lodge.routed if e.text] == ["after the buttons"]
    # The well-formed unknown id was a final 200 (the router answers it), never a 400 that holds the cursor.
    assert [m for m, p in api.calls if m == "answerCallbackQuery"] == ["answerCallbackQuery"] * 2
