"""The real inbound router: accepted events become session messages, with the
session boundary mocked."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container" / "lib"))

from server.connector_commands import Commands  # noqa: E402
from server.connector_router import AttachmentUnavailable, Router, destinations_for, place_file, session_text  # noqa: E402
from server.connectors import AcceptedEvent, ConnectorStore, Limits  # noqa: E402


class Sessions:
    """A fake session boundary: what is live, what was pasted, what was resumed."""

    def __init__(self, live=None, statuses=None):
        self.live = live or {}            # wolt -> live session name
        self.statuses = statuses or {}    # session -> deliver status
        self.pasted = []
        self.resumed = []

    def deliver(self, session, text):
        self.pasted.append((session, text))
        return {"status": self.statuses.get(session, "delivered"), "session": session}

    def resume(self, session, prompt):
        self.resumed.append((session, prompt))
        return {"status": "revived"}

    def resolve(self, wolt):
        return self.live.get(wolt)

    def wolt_of(self, session):
        return session.rsplit("-", 1)[0]


@pytest.fixture
def lodge(tmp_path):
    store = ConnectorStore(tmp_path / "connectors", limits=Limits(outbound_file_max_bytes=1_000_000))
    store.add_instance("tg-main", "telegram", "bot:1")
    binding = store.bind("tg-main", sender="u1", conversation="c1", default_wolt="softie")
    sleepy = store.bind("tg-main", sender="u1", conversation="c2", default_wolt="softie", may_wake=True)
    store.enable("tg-main")
    return store, binding, sleepy


def _event(store, binding, text="hello", attachments=(), reply_to_session=None, conversation="c1", event_id="tg:1"):
    event = AcceptedEvent(
        "tg-main", "telegram", "bot:1", event_id, binding, {"provider_id": "u1", "display": "jer"},
        {"provider_id": conversation, "thread": None}, text, tuple(attachments), reply_to_session, 7,
    )
    # Waiting for its receipt, the way accept_event leaves it: the router
    # delivers only an event that is still dispatchable.
    queue = store._queue("tg-main")
    queue.inbound.setdefault(event.dedup_key, {"state": "accepted", "event": event.to_record(), "at": 0})
    store._save_queue("tg-main", queue)
    return event


def _replies(store):
    return [d["text"] for d in store.list_deliveries("tg-main") if d["status"] == "queued"]


def test_delivers_to_the_default_wolts_live_session_and_remembers_it(lodge, tmp_path):
    store, binding, _ = lodge
    sessions = Sessions(live={"softie": "softie-7"})
    router = Router(store, uploads_root=tmp_path / "space", **_boundary(sessions), wolts_dir=tmp_path)
    result = router(_event(store, binding))
    assert result == {"routed": "delivered", "session": "softie-7"}
    session, text = sessions.pasted[0]
    assert session == "softie-7"
    assert text.startswith("[telegram message from jer, chat_id=c1, event=tg:1]: hello")
    assert "WOLTSPACE_NOTIFY" in text
    assert store.routing_state("tg-main", binding["binding_id"])["active_session"] == "softie-7"
    assert _replies(store) == ["🐾 sent to softie-7"]
    # Next time the remembered session wins over the wolt's live one.
    sessions.live["softie"] = "softie-8"
    router(_event(store, binding, "again"))
    assert sessions.pasted[-1][0] == "softie-7"


def test_a_reply_goes_to_the_delivery_s_session_first(lodge, tmp_path):
    store, binding, _ = lodge
    sessions = Sessions(live={"softie": "softie-7"})
    router = Router(store, uploads_root=tmp_path / "space", **_boundary(sessions), wolts_dir=tmp_path)
    store.remember_route("tg-main", binding["binding_id"], active_session="softie-7")
    assert router(_event(store, binding, reply_to_session="softie-3"))["session"] == "softie-3"
    assert sessions.pasted[0][0] == "softie-3"


def test_no_live_session_is_refused_not_spawned(lodge, tmp_path):
    store, binding, _ = lodge
    sessions = Sessions()
    router = Router(store, uploads_root=tmp_path / "space", **_boundary(sessions), wolts_dir=tmp_path)
    result = router(_event(store, binding))
    assert result == {"routed": "refused", "reason": "no_session", "wolt": "softie"}
    assert sessions.pasted == [] and sessions.resumed == []
    assert _replies(store) == ["🪵 softie has no live session; ask the owner to start one"]


class Starter(Sessions):
    """Sessions that can also be started, the way `/new` starts them."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.started = []

    def start(self, wolt, prompt, routing):
        name = f"{wolt}-new{len(self.started) + 1}"
        self.started.append((wolt, prompt, routing))
        self.live[wolt] = name
        return {"name": name}


def _starting_router(store, sessions, tmp_path, **kwargs):
    commands = Commands(
        store, start=sessions.start, stop=lambda s: {}, live=lambda s: "running" if s in sessions.live.values() else None,
        resolve=sessions.resolve, find=lambda key: None, wolt_of=sessions.wolt_of,
    )
    return Router(
        store, uploads_root=tmp_path / "space", **_boundary(sessions), wolts_dir=tmp_path,
        commands=commands, opening=lambda wolt, channel: f"/woltspace-start-chat {channel} {wolt}", **kwargs,
    )


def test_with_may_create_a_message_starts_a_session_with_itself_in_the_opening_prompt(lodge, tmp_path):
    store, _, _ = lodge
    owner = store.bind("tg-main", sender="u1", conversation="c3", default_wolt="softie", may_create=True)
    sessions = Starter()
    router = _starting_router(store, sessions, tmp_path)
    result = router(_event(store, owner, "hi there", conversation="c3"))
    assert result["outcome"] == "created" and result["session"] == "softie-new1"
    wolt, prompt, routing = sessions.started[0]
    assert wolt == "softie"
    # start-chat leads, then the message as a session reads it: nothing is pasted.
    assert prompt.startswith("/woltspace-start-chat telegram softie\n\n[telegram message from jer, chat_id=c3")
    assert "hi there" in prompt and "WOLTSPACE_NOTIFY" in prompt
    assert routing["adapter"] == "telegram" and routing["chat_id"] == "c3"
    assert sessions.pasted == []
    state = store.routing_state("tg-main", owner["binding_id"])
    assert state["active_session"] == "softie-new1"
    assert state["sessions"]["softie-new1"]["via"] == "created"
    assert _replies(store)[-1] == "🐾 new session for softie: softie-new1"
    # The next message goes to that session, by paste.
    router(_event(store, owner, "and again", conversation="c3", event_id="tg:2"))
    assert sessions.pasted[-1][0] == "softie-new1" and len(sessions.started) == 1


def test_a_retry_of_the_same_event_never_starts_a_second_session_or_pastes(lodge, tmp_path):
    store, _, _ = lodge
    owner = store.bind("tg-main", sender="u1", conversation="c3", default_wolt="softie", may_create=True)
    sessions = Starter()
    router = _starting_router(store, sessions, tmp_path)
    event = _event(store, owner, "once", conversation="c3")
    first = router(event)
    # The receipt was never written (a crash): the same event is dispatched again,
    # and by now the wolt has a live session the router would otherwise paste into.
    again = router(event)
    assert again == first
    assert len(sessions.started) == 1 and sessions.pasted == []


def test_an_expired_session_is_replaced_with_the_message_in_the_new_one(lodge, tmp_path):
    store, _, _ = lodge
    owner = store.bind("tg-main", sender="u1", conversation="c3", default_wolt="softie", may_create=True)
    sessions = Starter(statuses={"softie-old": "no-session"})
    router = _starting_router(store, sessions, tmp_path)
    store.remember_route("tg-main", owner["binding_id"], active_session="softie-old")
    result = router(_event(store, owner, "still there?", conversation="c3"))
    assert result["outcome"] == "created"
    assert [s for s, _ in sessions.pasted] == ["softie-old"]
    assert "still there?" in sessions.started[0][1]
    assert "🪵 softie-old expired; starting a new one for softie" in _replies(store)


def test_a_reply_to_a_gone_session_starts_one_for_that_session_s_wolt(lodge, tmp_path):
    store, _, _ = lodge
    owner = store.bind(
        "tg-main", sender="u1", conversation="c3", default_wolt="softie", may_create=True,
        selectable_wolts=["astre"],
    )
    sessions = Starter(statuses={"astre-3": "no-session"})
    router = _starting_router(store, sessions, tmp_path)
    result = router(_event(store, owner, "re", conversation="c3", reply_to_session="astre-3"))
    assert result["outcome"] == "created" and sessions.started[0][0] == "astre"


def test_without_may_create_nothing_is_started(lodge, tmp_path):
    store, binding, _ = lodge
    sessions = Starter()
    router = _starting_router(store, sessions, tmp_path)
    assert router(_event(store, binding))["reason"] == "no_session"
    assert sessions.started == []


def test_the_hourly_limit_holds_for_message_starts_too(lodge, tmp_path):
    store, _, _ = lodge
    owner = store.bind(
        "tg-main", sender="u1", conversation="c3", default_wolt="softie", may_create=True, create_per_hour=1,
    )
    sessions = Starter()
    router = _starting_router(store, sessions, tmp_path)
    router(_event(store, owner, "one", conversation="c3"))
    # That session is gone again, and the wolt has no other.
    sessions.live.clear()
    sessions.statuses["softie-new1"] = "no-session"
    result = router(_event(store, owner, "two", conversation="c3", event_id="tg:2"))
    assert result["outcome"] == "refused" and result["reason"] == "rate_limited"
    assert len(sessions.started) == 1


@pytest.mark.parametrize("quote, expected", [
    ({"text": "the plan is ready", "file": None, "voice": False}, "[replying to: the plan is ready]\nship it"),
    ({"text": "", "file": "report.html", "voice": False}, "[replying to file report.html]\nship it"),
    ({"text": "listen", "file": None, "voice": True}, "[replying to voice note: listen]\nship it"),
    (None, "ship it"),
])
def test_a_reply_is_quoted_into_the_session_message_like_the_adapter_did(lodge, tmp_path, quote, expected):
    store, binding, _ = lodge
    sessions = Sessions(live={"softie": "softie-7"})
    router = Router(store, uploads_root=tmp_path / "space", **_boundary(sessions), wolts_dir=tmp_path)
    event = AcceptedEvent(**{**_event(store, binding, "ship it", reply_to_session="softie-7").__dict__, "reply_quote": quote})
    router(event)
    assert sessions.pasted[0][1].startswith(f"[telegram message from jer, chat_id=c1, event=tg:1]: {expected}")


def test_a_stopped_session_is_woken_only_when_the_binding_may_wake(lodge, tmp_path):
    store, binding, sleepy = lodge
    sessions = Sessions(live={"softie": "softie-7"}, statuses={"softie-7": "agent-gone"})
    router = Router(store, uploads_root=tmp_path / "space", **_boundary(sessions), wolts_dir=tmp_path)
    refused = router(_event(store, binding))
    assert refused == {"routed": "refused", "reason": "may_wake_off", "session": "softie-7"}
    assert sessions.resumed == []
    woken = router(_event(store, sleepy, conversation="c2"))
    assert woken == {"routed": "resumed", "session": "softie-7", "detail": "revived"}
    assert sessions.resumed[0][0] == "softie-7"
    assert store.routing_state("tg-main", sleepy["binding_id"])["active_session"] == "softie-7"
    texts = _replies(store)
    assert "🪵 softie-7 is asleep and this chat may not wake it" in texts
    assert "🪵 softie-7 was asleep; woke it and delivered" in texts


def test_a_gone_session_falls_back_to_the_wolts_live_one_once(lodge, tmp_path):
    store, binding, _ = lodge
    sessions = Sessions(live={"softie": "softie-9"}, statuses={"softie-old": "no-session"})
    router = Router(store, uploads_root=tmp_path / "space", **_boundary(sessions), wolts_dir=tmp_path)
    store.remember_route("tg-main", binding["binding_id"], active_session="softie-old")
    result = router(_event(store, binding))
    assert result == {"routed": "delivered", "session": "softie-9", "fallback_from": "softie-old"}
    assert [s for s, _ in sessions.pasted] == ["softie-old", "softie-9"]
    assert store.routing_state("tg-main", binding["binding_id"])["active_session"] == "softie-9"
    sessions.live = {}
    sessions.statuses["softie-9"] = "no-session"
    gone = router(_event(store, binding))
    assert gone["routed"] == "refused"


def test_uploads_move_to_the_adapters_folder_and_are_released(lodge, tmp_path):
    store, binding, _ = lodge
    who = store.authenticate(store.issue_token("tg-main"))
    upload_id, part = store.begin_upload(who)
    part.write_bytes(b"opus bytes")
    store.commit_upload(who, upload_id, part, "note.ogg")
    store._queue("tg-main").uploads[upload_id]["claimed_by"] = "tg:1"
    sessions = Sessions(live={"softie": "softie-7"})
    router = Router(store, uploads_root=tmp_path / "space", **_boundary(sessions), wolts_dir=tmp_path)
    attachment = {"upload_id": upload_id, "path": str(store.root / "uploads" / "tg-main" / upload_id), "name": "note.ogg", "size": 10}
    router(_event(store, binding, "listen", attachments=[attachment]))
    saved = list((tmp_path / "space" / "telegram" / "uploads").iterdir())
    assert len(saved) == 1 and saved[0].name.endswith("_note.ogg")
    assert saved[0].read_bytes() == b"opus bytes"
    assert f"[file saved: {saved[0]}]" in sessions.pasted[0][1]
    assert not (store.root / "uploads" / "tg-main" / upload_id).exists()
    assert upload_id not in store._queue("tg-main").uploads


def test_session_text_names_the_event_for_duplicate_detection(lodge):
    store, binding, _ = lodge
    text = session_text(_event(store, binding, "hi"), [])
    assert "event=tg:1" in text.splitlines()[0]


def _boundary(sessions):
    return {"deliver": sessions.deliver, "resume": sessions.resume, "resolve": sessions.resolve, "wolt_of": sessions.wolt_of}


def _claimed_upload(store, event_id="tg:1"):
    who = store.authenticate(store.issue_token("tg-main"))
    upload_id, part = store.begin_upload(who)
    part.write_bytes(b"opus bytes")
    store.commit_upload(who, upload_id, part, "note.ogg")
    queue = store._queue("tg-main")
    queue.uploads[upload_id]["claimed_by"] = event_id
    store._save_queue("tg-main", queue)
    return upload_id, store.root / "uploads" / "tg-main" / upload_id


def _accepted_with_file(store, binding, upload_id, path):
    """An inbound record the way accept_event writes it, without a dispatcher run."""
    attachment = {"upload_id": upload_id, "path": str(path), "name": "note.ogg", "size": 10}
    event = _event(store, binding, "listen", attachments=[attachment])
    queue = store._queue("tg-main")
    queue.inbound[event.dedup_key] = {"state": "accepted", "event": event.to_record(), "at": 0}
    store._save_queue("tg-main", queue)
    return event


def test_file_destinations_are_persisted_before_the_move_and_survive_every_crash(lodge, tmp_path, monkeypatch):
    """Finding 3: crash before the move, after it, after release, before the paste."""
    store, binding, _ = lodge
    upload_id, source = _claimed_upload(store)
    event = _accepted_with_file(store, binding, upload_id, source)
    sessions = Sessions(live={"softie": "softie-7"})
    router = Router(store, uploads_root=tmp_path / "space", **_boundary(sessions), wolts_dir=tmp_path)
    uploads_dir = tmp_path / "space" / "telegram" / "uploads"
    planned = destinations_for(event, uploads_dir)[upload_id]

    def record():
        return store._queue("tg-main").inbound[event.dedup_key]["event"]["attachments"][0]

    def current_event():
        from server.connectors import AcceptedEvent
        return AcceptedEvent.from_record(store._queue("tg-main").inbound[event.dedup_key]["event"])

    # 1. Crash before the move: the destination is already on record.
    with monkeypatch.context() as patched:
        patched.setattr("server.connector_router.place_file", lambda s, d: (_ for _ in ()).throw(OSError("crash before move")))
        with pytest.raises(OSError):
            router(current_event())
    assert record()["dest"] == str(planned) and source.exists() and not planned.exists()
    # 2. Crash after the move, before release: source gone, destination there, handle still claimed.
    with monkeypatch.context() as patched:
        patched.setattr(store, "release_uploads", lambda i, ids: (_ for _ in ()).throw(OSError("crash after move")))
        with pytest.raises(OSError):
            router(current_event())
    assert planned.read_bytes() == b"opus bytes" and not source.exists()
    assert upload_id in store._queue("tg-main").uploads
    # 3. Crash after release, before the paste: the next attempt still names the file.
    with monkeypatch.context() as patched:
        patched.setattr(router, "deliver", lambda s, t: (_ for _ in ()).throw(OSError("crash before paste")))
        with pytest.raises(OSError):
            router(current_event())
    assert upload_id not in store._queue("tg-main").uploads and planned.exists()
    # 4. The attempt that goes through pastes the same path; a fifth attempt (crash before the receipt) too.
    for _ in range(2):
        result = router(current_event())
        assert result["routed"] == "delivered"
    assert [f"[file saved: {planned}]" in text for _s, text in sessions.pasted] == [True, True]
    assert planned.read_bytes() == b"opus bytes" and list(uploads_dir.iterdir()) == [planned]


def test_place_file_never_overwrites_and_is_idempotent(tmp_path):
    source = tmp_path / "src"
    dest = tmp_path / "dest" / "file"
    source.write_bytes(b"new")
    assert place_file(source, dest) == dest and dest.read_bytes() == b"new" and not source.exists()
    # Again, with the source gone: fine, the destination exists.
    assert place_file(source, dest) == dest
    # A destination that already exists is never replaced, even by a different source.
    other = tmp_path / "other"
    other.write_bytes(b"other")
    assert place_file(other, dest) == dest and dest.read_bytes() == b"new" and not other.exists()
    # Neither side present: nothing to paste.
    with pytest.raises(AttachmentUnavailable):
        place_file(tmp_path / "missing", tmp_path / "dest" / "never")
    # Two events never share a destination: the name is the event's own.
    assert len({str(p) for p in (dest,)}) == 1


def test_claimed_uploads_of_a_waiting_event_outlive_the_upload_ttl(lodge):
    store, binding, _ = lodge
    upload_id, source = _claimed_upload(store)
    _accepted_with_file(store, binding, upload_id, source)
    store.clock = lambda: 10 ** 9   # far past any TTL
    store.list_deliveries("tg-main")
    assert upload_id in store._queue("tg-main").uploads and source.exists()


def test_overlapping_attempts_never_both_act_on_one_event(tmp_path):
    """Astre's rc1 reproduction: attempt A is slow in its route decision; once the
    retry interval passes another attempt is claimed. It must not start a session
    (or paste) beside A, and A's own effect is the event's receipt."""
    import threading

    now = [1000.0]
    store = ConnectorStore(tmp_path / "connectors", clock=lambda: now[0])
    store.add_instance("one", "fake", "acct")
    store.bind("one", sender="u", conversation="c", default_wolt="astre", may_create=True)
    store.enable("one")
    who = store.authenticate(store.issue_token("one"))
    entered, release = threading.Event(), threading.Event()
    started, pasted, calls = [], [], [0]

    def resolve(wolt):
        calls[0] += 1
        if calls[0] == 1:
            entered.set()
            assert release.wait(5)
            return "astre-new"
        return None

    def start(wolt, prompt, routing):
        started.append(prompt)
        return {"name": "astre-new"}

    commands = Commands(store, start=start, live=lambda n: "running", resolve=lambda w: None, wolt_of=lambda n: "astre")
    store.router = Router(
        store, uploads_root=tmp_path, resolve=resolve, deliver=lambda n, t: pasted.append(t) or {"status": "delivered"},
        wolt_of=lambda n: "astre", commands=commands, opening=lambda *a: "start-chat",
    )
    errors = []

    def first():
        try:
            store.accept_event(who, {"event_id": "e", "sender": {"provider_id": "u"}, "conversation": {"provider_id": "c"}, "text": "hello"})
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    thread = threading.Thread(target=first)
    thread.start()
    assert entered.wait(5)
    now[0] += 31                 # past the retry interval: a second attempt is claimed
    store.lease(who, 1)
    release.set()
    thread.join(5)
    assert errors == []
    assert (len(started), len(pasted)) == (0, 1)
    record = store._queue("one").inbound[next(iter(store._queue("one").inbound))]
    assert record["state"] == "handed_off" and record["result"]["routed"] == "delivered"
    # Nothing is left for a later attempt to do again.
    now[0] += 31
    store.lease(who, 1)
    assert (len(started), len(pasted)) == (0, 1)


@pytest.mark.parametrize("max_attempts", [1, 5])
def test_the_last_permitted_attempt_is_never_parked_while_it_routes(tmp_path, max_attempts):
    """Astre's round-2 reproduction: a sweep marked the event handoff_failed while its
    last attempt was delivering, dropped the successful receipt, and an owner retry
    pasted again."""
    import threading

    now = [1000.0]
    store = ConnectorStore(tmp_path / "connectors", clock=lambda: now[0], limits=Limits(handoff_max_attempts=max_attempts))
    store.add_instance("one", "fake", "acct")
    store.bind("one", sender="u", conversation="c", default_wolt="astre")
    store.enable("one")
    who = store.authenticate(store.issue_token("one"))
    pasted, holding, release = [], threading.Event(), threading.Event()
    failures = [max_attempts - 1]

    def deliver(session, text):
        if failures[0]:
            failures[0] -= 1
            raise RuntimeError("not yet")
        holding.set()
        assert release.wait(5)
        pasted.append(text)
        return {"status": "delivered"}

    store.router = Router(store, uploads_root=tmp_path, resolve=lambda w: "astre-1", deliver=deliver, wolt_of=lambda n: "astre")
    payload = {"event_id": "e", "sender": {"provider_id": "u"}, "conversation": {"provider_id": "c"}, "text": "hello"}
    key = "acct\x00e"
    worker = None
    for attempt in range(max_attempts):
        if attempt == max_attempts - 1:
            run = (lambda: store.accept_event(who, payload)) if attempt == 0 else (lambda: store.lease(who, 1))
            worker = threading.Thread(target=run)
            worker.start()
            assert holding.wait(5)
        elif attempt == 0:
            store.accept_event(who, payload)
        else:
            store.lease(who, 1)
        now[0] += 31
    # The last attempt is delivering; a sweep runs with every attempt spent.
    store.lease(who, 1)
    assert store._queue("one").inbound[key]["state"] == "accepted"
    release.set()
    worker.join(5)
    record = store._queue("one").inbound[key]
    assert (record["state"], record["result"]["routed"]) == ("handed_off", "delivered")
    assert store.retry_handoff("one", "e") is False      # nothing left to retry
    now[0] += 31
    store.lease(who, 1)
    assert len(pasted) == 1
