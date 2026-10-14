"""The real inbound router: accepted events become session messages, with the
session boundary mocked."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container" / "lib"))

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


@pytest.fixture
def lodge(tmp_path):
    store = ConnectorStore(tmp_path / "connectors", limits=Limits(outbound_file_max_bytes=1_000_000))
    store.add_instance("tg-main", "telegram", "bot:1")
    binding = store.bind("tg-main", sender="u1", conversation="c1", default_wolt="softie")
    sleepy = store.bind("tg-main", sender="u1", conversation="c2", default_wolt="softie", may_wake=True)
    store.enable("tg-main")
    return store, binding, sleepy


def _event(store, binding, text="hello", attachments=(), reply_to_session=None, conversation="c1"):
    return AcceptedEvent(
        "tg-main", "telegram", "bot:1", "tg:1", binding, {"provider_id": "u1", "display": "jer"},
        {"provider_id": conversation, "thread": None}, text, tuple(attachments), reply_to_session, 7,
    )


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
    assert store.routing_state("tg-main", binding["binding_id"]) == {"active_session": "softie-7"}
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
    assert store.routing_state("tg-main", sleepy["binding_id"]) == {"active_session": "softie-7"}
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
    assert store.routing_state("tg-main", binding["binding_id"]) == {"active_session": "softie-9"}
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
    return {"deliver": sessions.deliver, "resume": sessions.resume, "resolve": sessions.resolve}


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
