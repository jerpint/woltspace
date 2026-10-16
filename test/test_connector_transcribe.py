"""Voice transcription through the connector router: owner opt-in, bounded, cached.
Fake provider only; no real API call anywhere."""

import fcntl
import json
import contextlib
import os
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container" / "lib"))

from server import connector_transcribe  # noqa: E402
from server.connector_router import Router  # noqa: E402
from server.connector_transcribe import Transcriber, openai_transcribe  # noqa: E402
from server.connectors import AcceptedEvent, ConnectorStore, Limits  # noqa: E402

SECRET = "sk-test-SECRETSECRETSECRET"


class Clock:
    def __init__(self):
        self.now = 1_700_000_000.0

    def __call__(self):
        return self.now


class Provider:
    def __init__(self, outcome=None):
        self.calls = []
        self.outcome = outcome or {"text": "hello from a voice note"}

    def __call__(self, data, filename, content_type, key, settings=None):
        self.calls.append((len(data), filename, content_type))
        self.settings, self.key = settings, key
        return dict(self.outcome) if not callable(self.outcome) else self.outcome()


class Sessions:
    def __init__(self):
        self.pasted, self.stopped = [], []

    def deliver(self, session, text):
        self.pasted.append((session, text))
        return {"status": "delivered"}

    def resolve(self, wolt):
        return f"{wolt}-1"

    def wolt_of(self, session):
        return session.rsplit("-", 1)[0]


@pytest.fixture
def lodge(tmp_path):
    clock = Clock()
    store = ConnectorStore(tmp_path / "connectors", clock=clock, limits=Limits(
        transcribe_max_bytes=1000, transcribe_bytes_per_day=5000, transcribe_requests_per_day=3,
        outbound_file_max_bytes=1_000_000,
    ))
    store.add_instance("tg-main", "telegram", "bot:1")
    binding = store.bind("tg-main", sender="u1", conversation="c1", default_wolt="softie", may_transcribe=True)
    store.enable("tg-main")
    provider, sessions = Provider(), Sessions()
    key = {"value": SECRET}
    space = tmp_path / "space"
    transcriber = Transcriber(store, space, call=provider, key=lambda: key["value"], slot_wait=0.2)
    router = Router(store, uploads_root=space, deliver=sessions.deliver, resolve=sessions.resolve,
                    wolts_dir=tmp_path, wolt_of=sessions.wolt_of, transcriber=transcriber)
    store.router = router
    return type("L", (), dict(store=store, binding=binding, provider=provider, sessions=sessions, key=key,
                              clock=clock, router=router, space=space, transcriber=transcriber, tmp=tmp_path))()


def _accepted(lodge, event_id="tg:1", body=b"opus bytes", name="voice_x.ogg"):
    """An inbound record with one claimed upload, the way accept_event leaves it."""
    store = lodge.store
    who = store.authenticate(store.issue_token("tg-main"))
    upload_id, part = store.begin_upload(who)
    part.write_bytes(body)
    store.commit_upload(who, upload_id, part, name)
    queue = store._queue("tg-main")
    queue.uploads[upload_id]["claimed_by"] = event_id
    path = store.root / "uploads" / "tg-main" / upload_id
    binding = store.binding_now(lodge.binding["binding_id"])
    event = AcceptedEvent(
        "tg-main", "telegram", "bot:1", event_id, binding, {"provider_id": "u1"},
        {"provider_id": "c1", "thread": None}, "",
        ({"upload_id": upload_id, "path": str(path), "name": name, "size": len(body)},), None, 7,
    )
    queue.inbound[event.dedup_key] = {"state": "accepted", "event": event.to_record(), "at": lodge.clock.now}
    store._save_queue("tg-main", queue)
    return event


def _pasted(lodge):
    return lodge.sessions.pasted[-1][1]


def test_a_granted_voice_note_is_transcribed_once_and_reused(lodge):
    event = _accepted(lodge)
    assert lodge.router(event)["routed"] == "delivered"
    assert "[voice note transcript]: hello from a voice note" in _pasted(lodge)
    assert lodge.provider.calls == [(10, "voice.ogg", "audio/ogg")]
    lodge.router(event)                                   # a paste retry
    assert len(lodge.provider.calls) == 1 and "hello from a voice note" in _pasted(lodge)


def test_a_skip_while_the_grant_was_off_stays_a_skip(lodge):
    lodge.store.bind("tg-main", sender="u1", conversation="c1", default_wolt="softie")
    event = _accepted(lodge)
    lodge.router(event)
    assert "not transcribed (not_enabled)" in _pasted(lodge) and lodge.provider.calls == []
    lodge.store.bind("tg-main", sender="u1", conversation="c1", default_wolt="softie", may_transcribe=True)
    lodge.router(event)
    assert "not transcribed (not_enabled)" in _pasted(lodge) and lodge.provider.calls == []


def test_no_key_means_zero_calls_and_no_charge(lodge):
    lodge.key["value"] = None
    lodge.router(_accepted(lodge))
    assert "not transcribed (no_key)" in _pasted(lodge) and lodge.provider.calls == []
    assert lodge.store._queue("tg-main").transcribe_budget.get("requests", 0) == 0


def test_bytes_are_measured_on_the_stored_file(lodge):
    lodge.router(_accepted(lodge, body=b"x" * 1001))
    assert "not transcribed (too_large)" in _pasted(lodge) and lodge.provider.calls == []


def test_the_daily_budget_charges_each_attempt_and_rolls_over_at_utc_midnight(lodge):
    for i in range(3):
        lodge.router(_accepted(lodge, event_id=f"tg:{i}"))
    lodge.router(_accepted(lodge, event_id="tg:3"))
    assert "not transcribed (over_budget)" in _pasted(lodge) and len(lodge.provider.calls) == 3
    budget = dict(lodge.store._queue("tg-main").transcribe_budget)
    lodge.clock.now += 86400
    lodge.router(_accepted(lodge, event_id="tg:4"))
    assert len(lodge.provider.calls) == 4
    new = lodge.store._queue("tg-main").transcribe_budget
    assert new["day"] != budget["day"] and new["requests"] == 1        # a new day, nothing refunded or moved


def test_an_active_claim_defers_the_handoff_without_spending_attempts(lodge):
    event = _accepted(lodge)
    claim = lodge.store.claim_transcription("tg-main", event.dedup_key, event.attachments[0]["upload_id"], 10)
    assert "claim" in claim
    queue = lodge.store._queue("tg-main")
    queue.inbound[event.dedup_key].update({"attempts": 1, "claim": "c1"})
    lodge.store._save_queue("tg-main", queue)
    for _ in range(8):                                    # many deferrals: never parked
        lodge.store._hand_off("tg-main", event.dedup_key, "c1")
        record = lodge.store._queue("tg-main").inbound[event.dedup_key]
        assert record["state"] == "accepted" and record["attempts"] == 0
        queue = lodge.store._queue("tg-main")
        queue.inbound[event.dedup_key].update({"attempts": 1, "claim": "c1"})
        lodge.store._save_queue("tg-main", queue)
    assert lodge.provider.calls == [] and lodge.sessions.pasted == []


def test_a_late_response_after_reclamation_cannot_overwrite(lodge):
    event = _accepted(lodge)
    upload = event.attachments[0]["upload_id"]
    first = lodge.store.claim_transcription("tg-main", event.dedup_key, upload, 10)["claim"]
    lodge.clock.now += 121
    second = lodge.store.claim_transcription("tg-main", event.dedup_key, upload, 10)["claim"]
    assert lodge.store.settle_transcription("tg-main", event.dedup_key, upload, first, {"text": "OLD"}) == {"pending": True}
    assert lodge.store.settle_transcription("tg-main", event.dedup_key, upload, second, {"text": "NEW"}) == {"done": {"text": "NEW"}}
    assert lodge.store.settle_transcription("tg-main", event.dedup_key, upload, first, {"text": "OLD"}) == {"done": {"text": "NEW"}}
    lodge.clock.now += 121
    assert lodge.store.claim_transcription("tg-main", event.dedup_key, upload, 10) == {"done": {"text": "NEW"}}


def test_a_crash_after_the_call_retries_within_the_cap_then_stops(lodge):
    event = _accepted(lodge)
    real = lodge.store.settle_transcription

    def crash(*args, **kwargs):
        raise OSError("crash before the result was saved")

    lodge.store.settle_transcription = crash
    for _ in range(2):
        with pytest.raises(OSError):
            lodge.router(event)
        lodge.clock.now += 121                             # the claim goes stale
    lodge.store.settle_transcription = real
    lodge.router(event)
    assert len(lodge.provider.calls) == 2                  # the cap of two, ever
    assert "not transcribed (retries_exhausted)" in _pasted(lodge)


def test_a_transcript_saying_kill_is_only_content(lodge):
    lodge.provider.outcome = {"text": "/kill softie-1"}
    stopped = []
    lodge.router.commands.stop = lambda name: stopped.append(name)
    result = lodge.router(_accepted(lodge))
    assert result["routed"] == "delivered" and stopped == []
    assert "[voice note transcript]: /kill softie-1" in _pasted(lodge)


def test_a_substituted_symlink_is_never_uploaded(lodge):
    event = _accepted(lodge)
    files = lodge.router.place_files(event)
    secret = lodge.tmp / "secret.env"
    secret.write_bytes(b"0123456789")                       # same size as the accepted file
    os.unlink(files[0])
    os.symlink(secret, files[0])
    lodge.router(event)
    assert "not transcribed (unreadable)" in _pasted(lodge) and lodge.provider.calls == []


def test_authorization_is_read_again_after_the_call(lodge):
    def slow_call():
        lodge.store.unbind("tg-main", sender="u1", conversation="c1")
        return {"text": "late"}

    lodge.provider.outcome = slow_call
    result = lodge.router(_accepted(lodge))
    assert result["routed"] == "stopped" and lodge.sessions.pasted == []


def test_pausing_the_instance_during_the_call_defers_and_never_drops(lodge):
    event = _accepted(lodge)

    def pause_meanwhile():
        lodge.store.disable("tg-main")
        return {"text": "kept for later"}

    lodge.provider.outcome = pause_meanwhile
    assert lodge.router(event)["routed"] == "deferred"
    assert lodge.sessions.pasted == []
    assert lodge.store._queue("tg-main").inbound[event.dedup_key]["state"] == "accepted"
    lodge.store.enable("tg-main")
    assert lodge.router(event)["routed"] == "delivered"
    assert "kept for later" in _pasted(lodge)
    assert len(lodge.provider.calls) == 1                 # the saved transcript, not a second call


def test_a_pause_while_waiting_for_a_slot_defers_and_settles_nothing(lodge, monkeypatch):
    """Paused after the slot is held but before the claim: no terminal skip, transcribed once back."""
    event = _accepted(lodge)
    real_slot = connector_transcribe.lodge_slot

    @contextlib.contextmanager
    def slot_then_pause(*args, **kwargs):
        with real_slot(*args, **kwargs):
            lodge.store.disable("tg-main")
            yield

    monkeypatch.setattr(connector_transcribe, "lodge_slot", slot_then_pause)
    assert lodge.router(event)["routed"] == "deferred"
    assert lodge.provider.calls == [] and lodge.sessions.pasted == []
    assert lodge.store._queue("tg-main").transcriptions == {}
    monkeypatch.setattr(connector_transcribe, "lodge_slot", real_slot)
    lodge.store.enable("tg-main")
    assert lodge.router(event)["routed"] == "delivered"
    assert len(lodge.provider.calls) == 1
    assert "not_allowed" not in _pasted(lodge)


def test_no_free_slot_defers(lodge):
    directory = lodge.store.root / "transcribe"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    held = []
    for index in range(2):
        fd = os.open(directory / f"slot-{index}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        held.append(fd)
    try:
        assert lodge.router(_accepted(lodge))["routed"] == "deferred"
        assert lodge.provider.calls == []
    finally:
        for fd in held:
            os.close(fd)


def test_delivered_bodies_become_tombstones_that_keep_the_count(lodge):
    event = _accepted(lodge)
    lodge.router(event)
    queue = lodge.store._queue("tg-main")
    queue.inbound[event.dedup_key]["state"] = "handed_off"
    lodge.store._save_queue("tg-main", queue)
    lodge.store.list_deliveries("tg-main")                 # any read sweeps
    [record] = lodge.store._queue("tg-main").transcriptions.values()
    assert record["disposition"] == {"reason": "delivered"} and record["attempts"] == 1
    # A late result for the finished event restores nothing.
    upload = event.attachments[0]["upload_id"]
    assert lodge.store.settle_transcription("tg-main", event.dedup_key, upload, "x", {"text": "body"}) == {"stopped": True}


_REAL_CLIENT = httpx.AsyncClient


def _provider_transport(monkeypatch, handler):
    def client(**kwargs):
        return _REAL_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(connector_transcribe.httpx, "AsyncClient", client)


def test_provider_errors_are_fixed_reasons_never_the_body(monkeypatch):
    _provider_transport(monkeypatch, lambda request: httpx.Response(401, text=f"bad key {SECRET}"))
    assert openai_transcribe(b"x", "voice.ogg", "audio/ogg", SECRET) == {"reason": "provider_401"}
    _provider_transport(monkeypatch, lambda request: httpx.Response(302, headers={"location": "https://evil.example"}))
    assert openai_transcribe(b"x", "voice.ogg", "audio/ogg", SECRET) == {"reason": "provider_302"}


def test_an_oversized_response_is_refused(monkeypatch):
    _provider_transport(monkeypatch, lambda request: httpx.Response(200, stream=httpx.ByteStream(b"{" + b" " * 70000 + b"}")))
    assert openai_transcribe(b"x", "voice.ogg", "audio/ogg", SECRET) == {"reason": "response_too_large"}


def test_a_good_response_is_bounded_text_from_a_fixed_endpoint(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps({"text": "hi " * 3000}).encode()))

    _provider_transport(monkeypatch, handler)
    outcome = openai_transcribe(b"x", "voice.ogg", "audio/ogg", SECRET)
    assert len(outcome["text"]) == 4000
    assert str(seen[0].url) == "https://api.openai.com/v1/audio/transcriptions"
    assert b'filename="voice.ogg"' in seen[0].content


# ------------------------------------------ round 2: boundaries that hold


def test_cancelling_the_event_during_the_call_means_no_paste_and_no_new_claim(lodge):
    event = _accepted(lodge)

    def cancel_meanwhile():
        lodge.store.cancel_handoff("tg-main", event.event_id)
        return {"text": "too late"}

    lodge.provider.outcome = cancel_meanwhile
    assert lodge.router(event)["routed"] == "stopped"
    assert lodge.sessions.pasted == []
    assert lodge.store._queue("tg-main").inbound[event.dedup_key]["state"] == "cancelled"
    upload = event.attachments[0]["upload_id"]
    assert lodge.store.claim_transcription("tg-main", event.dedup_key, upload, 10) == {"stopped": True}
    assert len(lodge.provider.calls) == 1


@pytest.mark.parametrize("replacement_settles", [False, True])
def test_a_replaced_claim_defers_until_the_replacement_settles(lodge, replacement_settles):
    """Through _hand_off: the first claim goes stale and a replacement takes it; then the first returns."""
    event = _accepted(lodge)
    upload = event.attachments[0]["upload_id"]
    held = {}

    def first_call():
        lodge.clock.now += 121                                 # the first claim goes stale meanwhile
        held["claim"] = lodge.store.claim_transcription("tg-main", event.dedup_key, upload, 10)["claim"]
        if replacement_settles:
            lodge.store.settle_transcription("tg-main", event.dedup_key, upload, held["claim"], {"text": "NEW"})
        return {"text": "OLD"}

    lodge.provider.outcome = first_call
    queue = lodge.store._queue("tg-main")
    queue.inbound[event.dedup_key].update({"attempts": 1, "claim": "h1"})
    lodge.store._save_queue("tg-main", queue)
    routed = lodge.store._hand_off("tg-main", event.dedup_key, "h1")["routed"]
    if replacement_settles:
        # The replacement's result is a true terminal disposition: it is what gets pasted.
        assert routed == "delivered" and "[voice note transcript]: NEW" in _pasted(lodge)
        assert "OLD" not in _pasted(lodge)
        return
    assert routed == "deferred" and lodge.sessions.pasted == []
    record = lodge.store._queue("tg-main").inbound[event.dedup_key]
    assert record["state"] == "accepted" and record["attempts"] == 0
    lodge.store.settle_transcription("tg-main", event.dedup_key, upload, held["claim"], {"text": "NEW"})
    queue = lodge.store._queue("tg-main")
    queue.inbound[event.dedup_key].update({"attempts": 1, "claim": "h2"})
    lodge.store._save_queue("tg-main", queue)
    assert lodge.store._hand_off("tg-main", event.dedup_key, "h2")["routed"] == "delivered"
    assert "[voice note transcript]: NEW" in _pasted(lodge) and "OLD" not in _pasted(lodge)


def test_the_deadline_is_end_to_end_for_a_slow_body(monkeypatch):
    import asyncio

    monkeypatch.setattr(connector_transcribe, "CALL_DEADLINE_SECONDS", 0.5)

    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(5):
                await asyncio.sleep(0.3)
                yield b" "

    _provider_transport(monkeypatch, lambda request: httpx.Response(200, stream=Slow()))
    import time

    started = time.monotonic()
    assert openai_transcribe(b"x", "voice.ogg", "audio/ogg", SECRET) == {"reason": "timeout"}
    assert time.monotonic() - started < 1.0


def test_the_deadline_covers_slow_headers_too(monkeypatch):
    import asyncio

    monkeypatch.setattr(connector_transcribe, "CALL_DEADLINE_SECONDS", 0.3)

    async def slow(request):
        await asyncio.sleep(2)
        return httpx.Response(200, json={"text": "never"})

    _provider_transport(monkeypatch, slow)
    assert openai_transcribe(b"x", "voice.ogg", "audio/ogg", SECRET) == {"reason": "timeout"}


def test_an_encoded_response_is_refused_before_it_is_decoded(monkeypatch):
    _provider_transport(monkeypatch, lambda request: httpx.Response(200, headers={"content-encoding": "gzip"}, content=b"x"))
    assert openai_transcribe(b"x", "voice.ogg", "audio/ogg", SECRET) == {"reason": "bad_response"}


async def test_the_lodge_stays_responsive_while_a_transcription_holds_and_a_cancel_cleans_up(lodge, monkeypatch):
    """Isolated ASGI app, held fake provider: /state answers meanwhile; a cancelled request's worker still frees its slot."""
    import asyncio
    import threading

    import server.app as server_app
    from server import connector_api

    entered, release = threading.Event(), threading.Event()

    def held(*args):
        entered.set()
        release.wait(10)
        return {"text": "held"}

    lodge.transcriber.call = held
    monkeypatch.setattr(connector_api, "CONNECTORS_DIR", lodge.store.root)
    monkeypatch.setattr(connector_api, "_store", lodge.store)
    token = lodge.store.issue_token("tg-main")
    headers = {"Authorization": f"Bearer {token}"}
    transport = httpx.ASGITransport(app=server_app.app, client=("127.0.0.1", 1))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:7777") as client:
        up = await client.post("/connector/v1/uploads", params={"name": "voice_a.ogg"}, content=b"opus bytes", headers=headers)
        upload_id = up.json()["upload_id"]
        post = asyncio.create_task(client.post("/connector/v1/events", headers=headers, json={
            "event_id": "tg:1", "sender": {"provider_id": "u1"}, "conversation": {"provider_id": "c1"},
            "text": "", "attachments": [{"upload_id": upload_id}],
        }))
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.05)
        assert entered.is_set()
        state = await asyncio.wait_for(client.get("/connector/v1/state", headers=headers), 2)
        assert state.status_code == 200                      # the event loop is not blocked
        post.cancel()
        release.set()
        try:
            await post
        except asyncio.CancelledError:
            pass
    # The worker finished on its own: both slots are free again.
    directory = lodge.store.root / "transcribe"
    for _ in range(100):
        fds = []
        try:
            for index in range(2):
                fd = os.open(directory / f"slot-{index}.lock", os.O_RDWR | os.O_CREAT, 0o600)
                fds.append(fd)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            await asyncio.sleep(0.05)
        finally:
            for fd in fds:
                os.close(fd)
    else:
        raise AssertionError("a transcription slot stayed held")
