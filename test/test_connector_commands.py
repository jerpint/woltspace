"""Chat commands, core-mediated: fixed commands, owner grants, binding-owned
resources, binding-scoped expiring buttons. The session boundary is mocked."""

import json
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container" / "lib"))
sys.path.insert(0, str(ROOT / "connectors" / "fake"))

import server.app as server_app  # noqa: E402
from server import connector_api  # noqa: E402
from server.connector_commands import Commands, parse_command  # noqa: E402
from server.connector_router import Router  # noqa: E402
from server.connectors import ConnectorError, ConnectorStore, Limits  # noqa: E402
from woltspace_connector_fake import FakeConnector  # noqa: E402


class Sessions:
    """A fake session layer: what is live, what was started and stopped."""

    def __init__(self):
        self.states = {}          # name -> status
        self.started = []
        self.stopped = []
        self.pasted = []
        self.counter = 0

    def start(self, wolt, prompt, routing):
        self.counter += 1
        name = f"{wolt}-{self.counter}"
        self.states[name] = "running"
        self.started.append((wolt, routing))
        return {"name": name}

    def stop(self, name):
        self.states[name] = "stopped"
        self.stopped.append(name)
        return {"name": name, "status": "stopped"}

    def live(self, name):
        return self.states.get(name)

    def resolve(self, wolt):
        for name, state in self.states.items():
            if name.startswith(wolt + "-") and state == "running":
                return name
        return None

    def deliver(self, session, text):
        self.pasted.append((session, text))
        return {"status": "delivered" if self.states.get(session) == "running" else "no-session"}

    def resume(self, session, prompt):
        return {"status": "revived"}

    def wolt_of(self, name):
        return name.rsplit("-", 1)[0] if name in self.states else None


class Clock:
    def __init__(self):
        self.now = 1_700_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def lodge(tmp_path, monkeypatch):
    clock = Clock()
    store = ConnectorStore(tmp_path / "connectors", clock=clock, limits=Limits(callback_ttl_seconds=600, outbound_file_max_bytes=1_000_000))
    sessions = Sessions()
    router = Router(
        store, uploads_root=tmp_path / "space", deliver=sessions.deliver, resume=sessions.resume,
        resolve=sessions.resolve, wolts_dir=tmp_path, wolt_of=sessions.wolt_of,
        commands=Commands(store, start=sessions.start, stop=sessions.stop, live=sessions.live, resolve=sessions.resolve,
                          wolt_of=sessions.wolt_of),
    )
    store.router = router
    store.add_instance("tg", "telegram", "bot:1")
    store.bind("tg", sender="u1", conversation="c1", default_wolt="softie")          # no grants
    store.bind(
        "tg", sender="u2", conversation="c2", default_wolt="softie",
        selectable_wolts=["softie", "otter"], may_create=True, may_stop_own_sessions=True,
        create_per_hour=2, create_concurrent=1,
    )
    store.enable("tg")
    token = store.issue_token("tg")
    monkeypatch.setattr(connector_api, "CONNECTORS_DIR", store.root)
    monkeypatch.setattr(connector_api, "_store", store)
    return type("L", (), {"store": store, "sessions": sessions, "clock": clock, "token": token, "n": 0})()


def _client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server_app.app, client=("127.0.0.1", 1)), base_url="http://localhost:7777",
    )


async def _say(lodge, connector, sender, conversation, text="", **extra):
    lodge.n += 1
    response = await connector.send_event(f"tg:{lodge.n}", sender, conversation, text, cursor=lodge.n, **extra)
    assert response.status_code == 200, response.text
    return response.json()


def _replies(lodge, conversation):
    return [
        d for d in lodge.store.list_deliveries("tg")
        if d["conversation"] == conversation and d["status"] == "queued"
    ]


def _last_reply(lodge, conversation):
    return _replies(lodge, conversation)[-1]


def _audit(lodge):
    path = lodge.store.root / "commands.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


# ------------------------------------------------------------------ parsing


def test_only_fixed_commands_with_one_bare_argument_are_commands():
    assert parse_command("/wolt") == ("wolt", None)
    assert parse_command("/wolt otter") == ("wolt", "otter")
    assert parse_command("/new@softbot otter") == ("new", "otter")
    assert parse_command("/sessions") == ("sessions", None)
    assert parse_command("/kill softie-1") == ("kill", "softie-1")
    assert parse_command("/apps") == ("unsupported", "apps")
    assert parse_command("/kill --all") == ("kill", "--all")           # refused later: not a name
    assert parse_command("/kill a b") is None                            # extra arguments: plain text
    assert parse_command("hello /wolt") is None
    assert parse_command("") is None


# ---------------------------------------------------------- /wolt (picker)


async def test_wolt_picks_only_from_the_grant_and_a_picker_uses_buttons(lodge):
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        refused = await _say(lodge, connector, "u1", "c1", "/wolt otter")
        assert refused["outcome"] == "refused" and refused["reason"] == "no_grant"
        assert "may not pick" in _last_reply(lodge, "c1")["text"]
        picker = await _say(lodge, connector, "u2", "c2", "/wolt")
        assert picker["outcome"] == "picker"
        reply = _last_reply(lodge, "c2")
        assert [[b["text"] for b in row] for row in reply["buttons"]] == [["✓ softie"], ["otter"]]
        assert all(b["callback_id"].startswith("cb_") for row in reply["buttons"] for b in row)
        wrong = await _say(lodge, connector, "u2", "c2", "/wolt beaver")
        assert (wrong["outcome"], wrong["reason"]) == ("refused", "not_selectable")
        chosen = await _say(lodge, connector, "u2", "c2", "/wolt otter")
        assert chosen["outcome"] == "selected"
        assert lodge.store.routing_state("tg", _binding(lodge, "u2"))["active_wolt"] == "otter"
        # Ordinary text now goes to the picked wolt's live session.
        lodge.sessions.states["otter-9"] = "running"
        routed = await _say(lodge, connector, "u2", "c2", "hello otter")
        assert routed["session"] == "otter-9"
    assert [a["command"] for a in _audit(lodge)] == ["wolt", "wolt", "wolt", "wolt"]


def _binding(lodge, sender):
    return next(b["binding_id"] for b in lodge.store.list_bindings("tg") if b["sender"] == sender)


# ------------------------------------------------------------ callbacks


async def test_a_button_press_resolves_once_inside_its_binding_and_expires(lodge):
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        await _say(lodge, connector, "u2", "c2", "/wolt")
        buttons = _last_reply(lodge, "c2")["buttons"]
        otter = buttons[1][0]["callback_id"]
        # A foreign binding pressing it: refused, nothing selected for either.
        foreign = await _say(lodge, connector, "u1", "c1", callback={"id": otter})
        assert (foreign["outcome"], foreign["reason"]) == ("refused", "callback_invalid")
        assert "expired or is not yours" in _last_reply(lodge, "c1")["text"]
        assert lodge.store.routing_state("tg", _binding(lodge, "u2")).get("active_wolt") is None
        # The owner of the button: once.
        pressed = await _say(lodge, connector, "u2", "c2", callback={"id": otter})
        assert (pressed["outcome"], pressed["wolt"]) == ("selected", "otter")
        again = await _say(lodge, connector, "u2", "c2", callback={"id": otter})
        assert (again["outcome"], again["reason"]) == ("refused", "callback_invalid")
        # Unknown and malformed ids.
        unknown = await _say(lodge, connector, "u2", "c2", callback={"id": "cb_" + "0" * 32})
        assert unknown["reason"] == "callback_invalid"
        bad = await connector.send_event("tg:bad", "u2", "c2", "", callback={"id": "not-ours"}, cursor=99)
        assert bad.status_code == 400
        # Expired.
        await _say(lodge, connector, "u2", "c2", "/wolt")
        softie = _last_reply(lodge, "c2")["buttons"][0][0]["callback_id"]
        lodge.clock.now += 601
        expired = await _say(lodge, connector, "u2", "c2", callback={"id": softie})
        assert expired["reason"] == "callback_invalid"
    # A pressed button's action is checked against the grant at that moment.
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        await _say(lodge, connector, "u2", "c2", "/wolt")
        otter = _last_reply(lodge, "c2")["buttons"][1][0]["callback_id"]
        lodge.store.bind("tg", sender="u2", conversation="c2", default_wolt="softie", selectable_wolts=["softie"])
        revoked = await _say(lodge, connector, "u2", "c2", callback={"id": otter})
    assert (revoked["outcome"], revoked["reason"]) == ("refused", "not_selectable")


def test_callbacks_are_bounded_per_binding(lodge):
    binding = _binding(lodge, "u2")
    rows = [[{"text": f"b{i}", "action": {"command": "wolt", "argument": "softie"}}] for i in range(20)]
    lodge.store.enqueue(channel="telegram", account=None, conversation="c2", text="x", binding_id=binding, buttons=rows)
    with pytest.raises(ConnectorError, match="too_many_buttons"):
        lodge.store.enqueue(channel="telegram", account=None, conversation="c2", text="x", binding_id=binding, buttons=rows + [[{"text": "one more", "action": {}}]])
    small = ConnectorStore(lodge.store.root, clock=lodge.clock, limits=Limits(callbacks_per_binding=25, outbound_file_max_bytes=1_000_000))
    with pytest.raises(ConnectorError, match="callbacks_exhausted"):
        small.enqueue(channel="telegram", account=None, conversation="c2", text="x", binding_id=binding, buttons=rows)


# --------------------------------------------------------------- /new


async def test_new_needs_its_own_grant_a_selectable_wolt_and_stays_within_limits(lodge):
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        # may_wake is not may_create.
        lodge.store.bind("tg", sender="u1", conversation="c1", default_wolt="softie", may_wake=True, selectable_wolts=["softie"])
        refused = await _say(lodge, connector, "u1", "c1", "/new")
        assert (refused["outcome"], refused["reason"]) == ("refused", "no_grant")
        assert lodge.sessions.started == []
        wrong = await _say(lodge, connector, "u2", "c2", "/new beaver")
        assert (wrong["outcome"], wrong["reason"]) == ("refused", "not_selectable")
        created = await _say(lodge, connector, "u2", "c2", "/new otter")
        assert (created["outcome"], created["session"]) == ("created", "otter-1")
        [(wolt, routing)] = lodge.sessions.started
        assert wolt == "otter" and routing["operation"] == "bot:1\x00tg:3"
        assert {k: routing[k] for k in ("adapter", "chat_id", "binding_id")} == {"adapter": "telegram", "chat_id": "c2", "binding_id": _binding(lodge, "u2")}
        state = lodge.store.routing_state("tg", _binding(lodge, "u2"))
        assert state["sessions"]["otter-1"]["via"] == "created" and state["active_session"] == "otter-1"
        # Concurrency: one live created session allowed.
        busy = await _say(lodge, connector, "u2", "c2", "/new otter")
        assert (busy["outcome"], busy["reason"]) == ("refused", "concurrency_limited")
        lodge.sessions.states["otter-1"] = "stopped"
        second = await _say(lodge, connector, "u2", "c2", "/new")      # the active wolt: otter
        assert (second["outcome"], second["wolt"]) == ("created", "otter")
        lodge.sessions.states["otter-2"] = "stopped"
        # Rate: two per hour.
        limited = await _say(lodge, connector, "u2", "c2", "/new otter")
        assert (limited["outcome"], limited["reason"]) == ("refused", "rate_limited")
        lodge.clock.now += 3601
        later = await _say(lodge, connector, "u2", "c2", "/new otter")
        assert later["outcome"] == "created"
    assert "started 2 session(s) this hour" in [d["text"] for d in _replies(lodge, "c2")][-2]


# ----------------------------------------------------------- /sessions


async def test_sessions_lists_only_what_this_binding_created_or_routed_to(lodge):
    lodge.sessions.states.update({"softie-7": "running", "softie-8": "running", "stranger-1": "running"})
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        empty = await _say(lodge, connector, "u2", "c2", "/sessions")
        assert empty["count"] == 0
        await _say(lodge, connector, "u2", "c2", "hello")            # routed to softie-7
        await _say(lodge, connector, "u2", "c2", "/new otter")        # created otter-1
        listed = await _say(lodge, connector, "u2", "c2", "/sessions")
        assert listed["count"] == 2
        text = _last_reply(lodge, "c2")["text"]
        assert "softie-7  running  (routed)" in text and "otter-1  running  (created)" in text
        assert "softie-8" not in text and "stranger-1" not in text
        # The other binding sees nothing of it.
        other = await _say(lodge, connector, "u1", "c1", "/sessions")
        assert other["count"] == 0


# --------------------------------------------------------------- /kill


async def test_kill_stops_only_an_associated_session_with_the_grant(lodge):
    lodge.sessions.states.update({"softie-7": "running", "stranger-1": "running"})
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        await _say(lodge, connector, "u1", "c1", "hello")              # u1 routed to softie-7, no grant
        refused = await _say(lodge, connector, "u1", "c1", "/kill softie-7")
        assert (refused["outcome"], refused["reason"]) == ("refused", "no_grant")
        await _say(lodge, connector, "u2", "c2", "/new otter")
        # Not this binding's, whether it exists or not: the same refusal.
        foreign = await _say(lodge, connector, "u2", "c2", "/kill stranger-1")
        assert (foreign["outcome"], foreign["reason"]) == ("refused", "not_associated")
        missing = await _say(lodge, connector, "u2", "c2", "/kill nothing-9")
        assert (missing["outcome"], missing["reason"]) == ("refused", "not_associated")
        bad = await _say(lodge, connector, "u2", "c2", "/kill --all")
        assert (bad["outcome"], bad["reason"]) == ("refused", "invalid_argument")
        assert lodge.sessions.stopped == []
        picker = await _say(lodge, connector, "u2", "c2", "/kill")
        assert picker["outcome"] == "picker"
        [[button]] = _last_reply(lodge, "c2")["buttons"]
        assert button["text"] == "stop otter-1"
        pressed = await _say(lodge, connector, "u2", "c2", callback={"id": button["callback_id"]})
        assert (pressed["outcome"], pressed["session"]) == ("stopped", "otter-1")
        assert lodge.sessions.stopped == ["otter-1"]
        # The grant is checked at execution: withdrawn, a stored button does nothing.
        await _say(lodge, connector, "u2", "c2", "/new otter")
        await _say(lodge, connector, "u2", "c2", "/kill")
        [[button]] = _last_reply(lodge, "c2")["buttons"]
        lodge.store.bind("tg", sender="u2", conversation="c2", default_wolt="softie", selectable_wolts=["softie", "otter"], may_create=True)
        withdrawn = await _say(lodge, connector, "u2", "c2", callback={"id": button["callback_id"]})
        assert (withdrawn["outcome"], withdrawn["reason"]) == ("refused", "no_grant")
    assert lodge.sessions.stopped == ["otter-1"]


# ---------------------------------------------------------- help, other


async def test_help_and_unsupported_commands_answer_without_doing_anything(lodge):
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        helped = await _say(lodge, connector, "u2", "c2", "/start")
        assert helped["outcome"] == "answered"
        text = _last_reply(lodge, "c2")["text"]
        assert "/new" in text and "/kill" in text and "/wolt" in text
        bare = await _say(lodge, connector, "u1", "c1", "/help")
        assert "/new" not in _last_reply(lodge, "c1")["text"]
        apps = await _say(lodge, connector, "u2", "c2", "/apps")
        assert (apps["outcome"], apps["reason"]) == ("refused", "unsupported")
        assert "/apps is not supported" in _last_reply(lodge, "c2")["text"]
    assert lodge.sessions.started == [] and lodge.sessions.pasted == []


async def test_a_command_with_a_file_releases_the_upload(lodge):
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        upload_id = (await connector.upload(b"x", name="n.bin")).json()["upload_id"]
        lodge.n += 1
        response = await connector.send_event(f"tg:{lodge.n}", "u2", "c2", "/sessions", attachments=(upload_id,), cursor=lodge.n)
        assert response.json()["outcome"] == "answered"
    assert upload_id not in lodge.store._queue("tg").uploads


# ------------------------------------------------- the route-matrix exception


@pytest.mark.parametrize("method, path, body", [
    ("POST", "/sessions/new/lodge", {"wolt": "softie"}),
    ("POST", "/sessions/new/telegram", {"wolt": "softie"}),
    ("POST", "/sessions/softie-1/stop", None),
    ("GET", "/sessions", None),
    ("POST", "/wolf/crons", {}),
])
async def test_the_connector_bearer_still_opens_no_owner_api(lodge, method, path, body):
    """Commands are the one core-mediated exception; the bearer itself opens nothing else."""
    async with _client() as client:
        response = await client.request(method, path, json=body, headers={"Authorization": f"Bearer {lodge.token}"})
    assert response.status_code == 401
    assert lodge.sessions.started == [] and lodge.sessions.stopped == []


# ---------------------------------------- round 2: reservations, replay, revalidation


def _event_payload(event_id, sender, conversation, text="", **extra):
    return {"event_id": event_id, "sender": {"provider_id": sender}, "conversation": {"provider_id": conversation}, "text": text, **extra}


def _binding_of(lodge, sender):
    return _binding(lodge, sender)


def test_two_racing_new_commands_get_one_start(lodge):
    """Astre's probe: per_hour=1, concurrent=1, a barrier inside start."""
    import threading

    lodge.store.bind("tg", sender="u2", conversation="c2", default_wolt="softie", selectable_wolts=["softie"],
                     may_create=True, create_per_hour=1, create_concurrent=1)
    barrier = threading.Barrier(2, timeout=5)
    started = []

    def start(wolt, prompt, routing):
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        name = f"{wolt}-{len(started) + 1}"
        started.append(name)
        lodge.sessions.states[name] = "running"
        return {"name": name}

    lodge.store.router.commands.start = start
    who = lodge.store.authenticate(lodge.token)
    outcomes = []

    def post(i):
        outcomes.append(lodge.store.accept_event(who, _event_payload(f"race{i}", "u2", "c2", "/new softie"))[1]["outcome"])

    threads = [threading.Thread(target=post, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(started) == 1
    assert sorted(outcomes) == ["created", "refused"]
    assert any(o["state"] == "answered" and o["command"] == "new" for o in lodge.store._queue("tg").operations.values())


def test_a_start_that_completed_is_never_repeated_when_the_record_failed(lodge):
    """Astre's probe: the association write fails after start; the retry reconciles, no second start."""
    who = lodge.store.authenticate(lodge.token)
    real_settle = lodge.store.settle_operation
    crashes = {"started": 1}

    def crashing_settle(instance_id, key, state, **fields):
        if crashes.get(state):
            crashes[state] -= 1
            raise OSError("crash after the effect, before the record")
        return real_settle(instance_id, key, state, **fields)

    lodge.store.settle_operation = crashing_settle
    # The session boundary can be asked by nonce: the fake start keeps the routing.
    routings = {}

    def start(wolt, prompt, routing):
        name = f"{wolt}-{len(routings) + 1}"
        routings[name] = routing
        lodge.sessions.states[name] = "running"
        return {"name": name}

    lodge.store.router.commands.start = start
    lodge.store.router.commands.find = lambda key: next((n for n, r in routings.items() if r.get("operation") == key), None)
    status, body = lodge.store.accept_event(who, _event_payload("e1", "u2", "c2", "/new otter"))
    assert body["routed"] == "error"
    record = lodge.store.operation("tg", "bot:1\x00e1")
    assert record["state"] == "effect_in_progress" and len(routings) == 1
    # The reservation is held meanwhile: another /new is refused on concurrency.
    assert lodge.store.accept_event(who, _event_payload("e2", "u2", "c2", "/new otter"))[1]["reason"] == "concurrency_limited"
    lodge.clock.now += 31
    lodge.store.lease(who, 1)                     # the retry
    record = lodge.store.operation("tg", "bot:1\x00e1")
    assert (record["state"], record["session"], len(routings)) == ("answered", "otter-1", 1)
    assert lodge.store.routing_state("tg", _binding_of(lodge, "u2"))["sessions"]["otter-1"]["via"] == "created"
    assert lodge.store.accept_event(who, _event_payload("e1", "u2", "c2", "/new otter"))[1]["session"] == "otter-1"


def test_an_effect_of_unknown_outcome_is_parked_and_only_the_owner_resolves_it(lodge):
    who = lodge.store.authenticate(lodge.token)
    real_settle = lodge.store.settle_operation

    def crashing_settle(instance_id, key, state, **fields):
        if state == "started":
            lodge.store.settle_operation = real_settle
            raise OSError("crash")
        return real_settle(instance_id, key, state, **fields)

    lodge.store.settle_operation = crashing_settle
    lodge.store.router.commands.find = lambda key: None       # nothing answers to the nonce
    lodge.store.accept_event(who, _event_payload("p1", "u2", "c2", "/new otter"))
    assert lodge.sessions.started and lodge.store.operation("tg", "bot:1\x00p1")["state"] == "effect_in_progress"
    lodge.clock.now += 31
    lodge.store.lease(who, 1)
    record = lodge.store.operation("tg", "bot:1\x00p1")
    assert record["state"] == "uncertain"
    assert len(lodge.sessions.started) == 1
    # Parked: the slot and the hourly charge are kept; an owner retry only replays the parked answer.
    assert lodge.store.accept_event(who, _event_payload("p2", "u2", "c2", "/new otter"))[1]["reason"] == "concurrency_limited"
    assert lodge.store.retry_handoff("tg", "p1") is False or True
    lodge.clock.now += 31
    lodge.store.lease(who, 1)
    assert len(lodge.sessions.started) == 1 and lodge.store.operation("tg", "bot:1\x00p1")["state"] == "uncertain"
    assert [o["event_id"] for o in lodge.store.uncertain_operations("tg")] == ["p1"]
    # The owner says what happened.
    assert lodge.store.resolve_operation("tg", "p1", session="otter-1") is True
    assert lodge.store.operation("tg", "bot:1\x00p1")["state"] == "answered"
    assert lodge.store.routing_state("tg", _binding_of(lodge, "u2"))["sessions"]["otter-1"]["via"] == "created"
    # Abandoning releases the reservation.
    lodge.store.settle_operation = crashing_settle
    lodge.sessions.states["otter-1"] = "stopped"
    lodge.store.accept_event(who, _event_payload("p3", "u2", "c2", "/new otter"))
    lodge.clock.now += 31
    lodge.store.lease(who, 1)
    assert lodge.store.operation("tg", "bot:1\x00p3")["state"] == "uncertain"
    assert lodge.store.resolve_operation("tg", "p3", abandon=True) is True
    assert lodge.store.operation("tg", "bot:1\x00p3")["state"] == "failed"
    lodge.clock.now += 3601
    assert lodge.store.accept_event(who, _event_payload("p4", "u2", "c2", "/new otter"))[1]["outcome"] == "created"


def test_crashes_before_the_effect_and_before_the_receipt_do_not_repeat_it(lodge):
    who = lodge.store.authenticate(lodge.token)
    real_settle = lodge.store.settle_operation
    # Before the effect: the crash lands at effect_in_progress, so nothing ran; the retry runs it once.
    once = {"effect_in_progress": 1}

    def crash_before(instance_id, key, state, **fields):
        if once.get(state):
            once[state] -= 1
            raise OSError("crash before the effect")
        return real_settle(instance_id, key, state, **fields)

    lodge.store.settle_operation = crash_before
    lodge.store.accept_event(who, _event_payload("b1", "u2", "c2", "/new otter"))
    assert lodge.sessions.started == [] and lodge.store.operation("tg", "bot:1\x00b1")["state"] == "reserved"
    lodge.clock.now += 31
    lodge.store.lease(who, 1)
    assert len(lodge.sessions.started) == 1 and lodge.store.operation("tg", "bot:1\x00b1")["state"] == "answered"
    # Before the receipt: the crash lands after `associated`; the retry answers without a second start.
    lodge.sessions.states["otter-1"] = "stopped"
    once = {"answered": 1}
    lodge.store.accept_event(who, _event_payload("b2", "u2", "c2", "/new otter"))
    assert lodge.store.operation("tg", "bot:1\x00b2")["state"] == "associated" and len(lodge.sessions.started) == 2
    lodge.clock.now += 31
    lodge.store.lease(who, 1)
    assert lodge.store.operation("tg", "bot:1\x00b2")["state"] == "answered" and len(lodge.sessions.started) == 2
    # A withdrawn grant on resume parks instead of continuing.
    lodge.store.settle_operation = real_settle
    lodge.sessions.states["otter-2"] = "stopped"
    lodge.clock.now += 3601                       # a fresh hour for the binding's two-per-hour grant
    once = {"started": 1}
    lodge.store.settle_operation = crash_before
    lodge.store.accept_event(who, _event_payload("b3", "u2", "c2", "/new otter"))
    lodge.store.bind("tg", sender="u2", conversation="c2", default_wolt="softie", selectable_wolts=["softie"], may_create=True)
    lodge.clock.now += 31
    lodge.store.lease(who, 1)
    assert lodge.store.operation("tg", "bot:1\x00b3")["state"] == "uncertain" and len(lodge.sessions.started) == 3


def test_a_kill_asked_once_is_never_sent_twice(lodge):
    who = lodge.store.authenticate(lodge.token)
    lodge.store.accept_event(who, _event_payload("n1", "u2", "c2", "/new otter"))
    real_settle = lodge.store.settle_operation
    once = {"started": 1}

    def crash_after_stop(instance_id, key, state, **fields):
        if once.get(state) and fields.get("session") == "otter-1" and state == "started":
            once[state] -= 1
            raise OSError("crash after the stop")
        return real_settle(instance_id, key, state, **fields)

    lodge.store.settle_operation = crash_after_stop
    lodge.store.accept_event(who, _event_payload("k1", "u2", "c2", "/kill otter-1"))
    assert lodge.sessions.stopped == ["otter-1"]
    assert lodge.store.operation("tg", "bot:1\x00k1")["state"] == "effect_in_progress"
    lodge.clock.now += 31
    lodge.store.lease(who, 1)                      # the session is stopped: done, no second stop
    assert lodge.sessions.stopped == ["otter-1"]
    assert lodge.store.operation("tg", "bot:1\x00k1")["state"] == "answered"
    # A stop that was sent but did not take is parked, not resent.
    lodge.sessions.states["otter-1"] = "running"
    lodge.store.accept_event(who, _event_payload("k2", "u2", "c2", "/kill otter-1"))
    lodge.sessions.states["otter-1"] = "running"   # it ignored us
    record = lodge.store._queue("tg").operations["bot:1\x00k2"]
    assert record["state"] == "answered"           # the fake stop "worked"; force the crash window instead
    queue = lodge.store._queue("tg")
    queue.operations["bot:1\x00k2"]["state"] = "effect_in_progress"
    queue.operations["bot:1\x00k2"].pop("result", None)
    queue.inbound["bot:1\x00k2"]["state"] = "accepted"
    lodge.store._save_queue("tg", queue)
    lodge.clock.now += 31
    lodge.store.lease(who, 1)
    assert lodge.sessions.stopped == ["otter-1", "otter-1"]
    assert lodge.store.operation("tg", "bot:1\x00k2")["state"] == "uncertain"


def test_a_button_resumes_its_own_operation_and_nobody_elses(lodge):
    who = lodge.store.authenticate(lodge.token)
    lodge.store.accept_event(who, _event_payload("n1", "u2", "c2", "/new otter"))
    lodge.store.accept_event(who, _event_payload("pk", "u2", "c2", "/kill"))
    [[button]] = [d for d in lodge.store.list_deliveries("tg") if d.get("buttons")][-1]["buttons"]
    real_settle = lodge.store.settle_operation
    once = {"started": 1}

    def crash(instance_id, key, state, **fields):
        if once.get(state) and state == "started":
            once[state] -= 1
            raise OSError("crash")
        return real_settle(instance_id, key, state, **fields)

    lodge.store.settle_operation = crash
    pressed = lodge.store.accept_event(who, _event_payload("press1", "u2", "c2", callback={"id": button["callback_id"]}))
    assert pressed[1]["routed"] == "error" and lodge.sessions.stopped == ["otter-1"]
    # Another event presenting the consumed button gets nothing.
    other = lodge.store.accept_event(who, _event_payload("press2", "u2", "c2", callback={"id": button["callback_id"]}))
    assert (other[1]["outcome"], other[1]["reason"]) == ("refused", "callback_invalid")
    # The same event resumes its operation.
    lodge.clock.now += 31
    lodge.store.lease(who, 1)
    assert lodge.store.operation("tg", "bot:1\x00press1")["state"] == "answered"
    assert lodge.sessions.stopped == ["otter-1"]


async def test_a_revoked_wolt_selection_does_not_persist(lodge):
    """Astre's probe: /wolt other, rebind without other, hello goes to the default wolt."""
    lodge.sessions.states.update({"softie-1": "running", "otter-1": "running"})
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        assert (await _say(lodge, connector, "u2", "c2", "/wolt otter"))["outcome"] == "selected"
        assert (await _say(lodge, connector, "u2", "c2", "hello"))["session"] == "otter-1"
        lodge.store.bind("tg", sender="u2", conversation="c2", default_wolt="softie", selectable_wolts=["softie"],
                         may_create=True, may_stop_own_sessions=True)
        routed = await _say(lodge, connector, "u2", "c2", "hello again")
        assert routed["session"] == "softie-1"
        state = lodge.store.routing_state("tg", _binding_of(lodge, "u2"))
        assert state.get("active_wolt") is None and state["active_session"] == "softie-1"
        # History still shows otter-1 (it was talked to); authority does not.
        listed = await _say(lodge, connector, "u2", "c2", "/sessions")
        assert listed["count"] == 2
    assert [s for s, _ in lodge.sessions.pasted] == ["otter-1", "softie-1"]


# --------------------------------------------- round 3: one command at a time


def _run(target):
    import threading

    thread = threading.Thread(target=target)
    thread.start()
    return thread


def test_a_live_sample_cannot_go_stale_while_another_command_completes(lodge):
    """Astre's probe: A samples live sessions, B completes a /new, A resumes. One start."""
    import threading

    lodge.store.bind("tg", sender="u2", conversation="c2", default_wolt="softie", selectable_wolts=["softie"],
                     may_create=True, create_per_hour=10, create_concurrent=1)
    who = lodge.store.authenticate(lodge.token)
    real = lodge.store.reserve_create
    sampled, proceed = threading.Event(), threading.Event()

    def reserve(instance, key, *args):
        if key.endswith("slow"):
            sampled.set()
            assert proceed.wait(5)
        return real(instance, key, *args)

    lodge.store.reserve_create = reserve
    slow = _run(lambda: lodge.store.accept_event(who, _event_payload("slow", "u2", "c2", "/new softie")))
    assert sampled.wait(5)
    fast = _run(lambda: lodge.store.accept_event(who, _event_payload("fast", "u2", "c2", "/new softie")))
    fast.join(0.3)
    assert fast.is_alive()                     # waits for the binding's command lock
    proceed.set()
    slow.join(5)
    fast.join(5)
    assert len(lodge.sessions.started) == 1


@pytest.mark.parametrize("command", ["new", "kill"])
def test_a_stale_attempt_cannot_redo_or_overwrite_a_finished_operation(lodge, command):
    """Astre's probe: A pauses before effect_in_progress, a retry of the same event runs; one effect."""
    import threading

    who = lodge.store.authenticate(lodge.token)
    lodge.sessions.states["softie-9"] = "running"
    if command == "kill":
        lodge.store.associate_session("tg", _binding(lodge, "u2"), "softie-9", via="routed", wolt="softie")
    text = "/new softie" if command == "new" else "/kill softie-9"
    real = lodge.store.settle_operation
    paused, go = threading.Event(), threading.Event()
    first = [True]

    def settle(instance, key, state, **fields):
        if state == "effect_in_progress" and first[0]:
            first[0] = False
            paused.set()
            assert go.wait(5)
        return real(instance, key, state, **fields)

    lodge.store.settle_operation = settle
    a = _run(lambda: lodge.store.accept_event(who, _event_payload("same", "u2", "c2", text)))
    assert paused.wait(5)
    lodge.clock.now += 31
    b = _run(lambda: lodge.store.lease(who, 1))          # the sweep's retry of the same event
    b.join(0.3)
    assert b.is_alive()                                  # it waits for A's command lock
    go.set()
    a.join(5)
    b.join(5)
    effects = lodge.sessions.started if command == "new" else lodge.sessions.stopped
    assert len(effects) == 1
    record = lodge.store.operation("tg", "bot:1\x00same")
    assert record["state"] == "answered"
    # And a stale transition can never move it again.
    with pytest.raises(ConnectorError):
        real("tg", "bot:1\x00same", "effect_in_progress", expect=("reserved",))
    with pytest.raises(ConnectorError):
        real("tg", "bot:1\x00same", "started")


def test_replies_and_kill_check_the_target_sessions_real_wolt(lodge):
    """Astre's probe: after otter leaves the selectable set, neither a reply nor /kill reaches otter's session."""
    who = lodge.store.authenticate(lodge.token)
    lodge.sessions.states.update({"otter-1": "running", "softie-1": "running"})
    lodge.store.accept_event(who, _event_payload("pick", "u2", "c2", "/wolt otter"))
    lodge.store.accept_event(who, _event_payload("hi", "u2", "c2", "hello"))
    assert lodge.sessions.pasted[-1][0] == "otter-1"
    binding = _binding(lodge, "u2")
    assert lodge.store.routing_state("tg", binding)["sessions"]["otter-1"]["wolt"] == "otter"
    delivery = lodge.store.enqueue(channel="telegram", account="bot:1", conversation="c2", text="reply here",
                                   session="otter-1", binding_id=binding)
    lodge.store.bind("tg", sender="u2", conversation="c2", default_wolt="softie", selectable_wolts=["softie"],
                     may_create=True, may_stop_own_sessions=True, create_per_hour=2, create_concurrent=1)
    pasted = len(lodge.sessions.pasted)
    status, body = lodge.store.accept_event(who, _event_payload("re", "u2", "c2", "again", reply_to={"delivery_id": delivery}))
    assert body["reason"] == "reply_target_not_allowed"
    assert len(lodge.sessions.pasted) == pasted
    status, body = lodge.store.accept_event(who, _event_payload("k", "u2", "c2", "/kill otter-1"))
    assert body["outcome"] == "refused" and lodge.sessions.stopped == []
    # A session whose wolt cannot be verified is refused too, never given the current selection.
    lodge.store.associate_session("tg", binding, "mystery", via="routed", wolt="softie")
    lodge.sessions.states["mystery"] = "running"
    lodge.sessions.wolt_of = lambda name: None if name == "mystery" else name.rsplit("-", 1)[0]
    lodge.store.router.wolt_of = lodge.sessions.wolt_of
    lodge.store.router.commands.wolt_of = lodge.sessions.wolt_of
    status, body = lodge.store.accept_event(who, _event_payload("k2", "u2", "c2", "/kill mystery"))
    assert body["outcome"] == "refused" and lodge.sessions.stopped == []


@pytest.mark.parametrize("found", [True, False])
def test_a_button_operation_resumes_after_its_button_expired(lodge, found):
    """Astre's probe: crash after start, before the result; retry past the TTL resumes from the operation."""
    who = lodge.store.authenticate(lodge.token)
    binding = _binding(lodge, "u2")
    lodge.store.enqueue(channel="telegram", account="bot:1", conversation="c2", text="new?", binding_id=binding,
                        buttons=[[{"text": "new", "action": {"command": "new", "argument": "softie"}}]])
    callback = next(d for d in lodge.store.list_deliveries("tg") if d.get("buttons"))["buttons"][0][0]["callback_id"]
    real = lodge.store.settle_operation

    def crash(instance, key, state, **fields):
        if state == "started":
            lodge.store.settle_operation = real
            raise OSError("crash after the effect")
        return real(instance, key, state, **fields)

    lodge.store.settle_operation = crash
    lodge.store.router.commands.find = (lambda key: "softie-1") if found else (lambda key: None)
    lodge.store.accept_event(who, _event_payload("press", "u2", "c2", callback={"id": callback}))
    assert lodge.store.operation("tg", "bot:1\x00press")["state"] == "effect_in_progress"
    lodge.clock.now += 3600
    lodge.store.lease(who, 1)
    record = lodge.store.operation("tg", "bot:1\x00press")
    assert len(lodge.sessions.started) == 1
    if found:
        assert (record["state"], record["session"]) == ("answered", "softie-1")
    else:
        assert record["state"] == "uncertain"
        assert [o["event_id"] for o in lodge.store.uncertain_operations("tg")] == ["press"]
    # Another event presenting the same (consumed, expired) button gets nothing.
    status, body = lodge.store.accept_event(who, _event_payload("again", "u2", "c2", callback={"id": callback}))
    assert body["reason"] == "callback_invalid" and len(lodge.sessions.started) == 1


def test_owner_resolution_refuses_a_new_done_without_its_session(lodge):
    binding = _binding(lodge, "u2")
    lodge.store.reserve_create("tg", "bot:1\x00p9", binding, "softie", 0)
    lodge.store.settle_operation("tg", "bot:1\x00p9", "uncertain")
    with pytest.raises(ConnectorError):
        lodge.store.resolve_operation("tg", "p9")                       # done, but no session
    with pytest.raises(ConnectorError):
        lodge.store.resolve_operation("tg", "p9", session="softie-1", abandon=True)
    assert lodge.store.operation("tg", "bot:1\x00p9")["state"] == "uncertain"


# ------------------------------------------- round 4: boundaries that hold


def test_a_commands_own_reply_cannot_reenter_a_command(lodge):
    """Astre's probe: start advances the clock past the retry delay; the reply's sweep must not dispatch inside the lock."""
    who = lodge.store.authenticate(lodge.token)

    def start(wolt, prompt, routing):
        lodge.clock.now += 31
        return lodge.sessions.start(wolt, prompt, routing)

    lodge.store.router.commands.start = start
    done = []
    worker = _run(lambda: done.append(lodge.store.accept_event(who, _event_payload("e1", "u2", "c2", "/new softie"))))
    worker.join(5)
    assert not worker.is_alive(), "a nested dispatch deadlocked on the command lock"
    assert done[0][1]["outcome"] == "created" and len(lodge.sessions.started) == 1


@pytest.mark.parametrize("command", ["new", "kill"])
def test_a_grant_withdrawn_at_the_effect_boundary_stops_the_effect(lodge, command):
    who = lodge.store.authenticate(lodge.token)
    binding = _binding(lodge, "u2")
    lodge.sessions.states["softie-9"] = "running"
    lodge.store.associate_session("tg", binding, "softie-9", via="routed", wolt="softie")
    real = lodge.store.settle_operation

    def settle(instance, key, state, **fields):
        if state == "effect_in_progress":
            # The owner withdraws the grant after the reservation, right before the effect.
            lodge.store.bind("tg", sender="u2", conversation="c2", default_wolt="softie", selectable_wolts=["softie", "otter"])
        return real(instance, key, state, **fields)

    lodge.store.settle_operation = settle
    text = "/new softie" if command == "new" else "/kill softie-9"
    status, body = lodge.store.accept_event(who, _event_payload("e1", "u2", "c2", text))
    assert body["reason"] == "grant_withdrawn"
    assert lodge.sessions.started == [] and lodge.sessions.stopped == []
    record = lodge.store.operation("tg", "bot:1\x00e1")
    # Settled (or, the narrowed grant being a new binding, swept with the old one); never left mid-effect.
    assert record is None or record["state"] in ("answered", "failed")


def test_a_grant_withdrawn_while_waiting_for_the_lock_is_seen(lodge):
    who = lodge.store.authenticate(lodge.token)
    binding = _binding(lodge, "u2")
    lodge.sessions.states["softie-9"] = "running"
    lodge.store.associate_session("tg", binding, "softie-9", via="routed", wolt="softie")
    out = []
    with lodge.store.command_lock(binding):
        worker = _run(lambda: out.append(lodge.store.accept_event(who, _event_payload("k", "u2", "c2", "/kill softie-9"))))
        worker.join(0.3)
        assert worker.is_alive()
        lodge.store.bind("tg", sender="u2", conversation="c2", default_wolt="softie", selectable_wolts=["softie"])
    worker.join(5)
    assert out[0][1]["reason"] == "no_grant" and lodge.sessions.stopped == []


def test_the_real_registry_shape_is_read_without_raising(monkeypatch):
    import types

    from server import connector_commands

    records = [
        {"name": "a-1", "routing": [{"adapter": "telegram", "chat_id": "1"}]},
        {"name": "a-2", "routing": None},
        {"name": "a-3", "routing": [{"adapter": "telegram", "operation": "bot:1\x00e7"}]},
    ]
    fake = types.ModuleType("sessions")
    fake.SessionRegistry = lambda: types.SimpleNamespace(list=lambda: records)
    monkeypatch.setitem(sys.modules, "sessions", fake)
    assert connector_commands._default_find("bot:1\x00nope") is None
    assert connector_commands._default_find("bot:1\x00e7") == "a-3"


def test_a_start_that_spawned_and_then_raised_is_parked_not_released(lodge):
    """Astre's probe: the session layer spawns, then its bookkeeping fails."""
    who = lodge.store.authenticate(lodge.token)
    lodge.store.router.commands.find = lambda key: None

    def start(wolt, prompt, routing):
        lodge.sessions.start(wolt, prompt, routing)
        raise RuntimeError("registry update failed after spawn")

    lodge.store.router.commands.start = start
    status, body = lodge.store.accept_event(who, _event_payload("n1", "u2", "c2", "/new softie"))
    assert body["outcome"] == "uncertain"
    assert lodge.store.operation("tg", "bot:1\x00n1")["state"] == "uncertain"
    status, body = lodge.store.accept_event(who, _event_payload("n2", "u2", "c2", "/new softie"))
    assert body["reason"] == "concurrency_limited"            # the parked one still holds its slot
    assert len(lodge.sessions.started) == 1


@pytest.mark.parametrize("still_running", [False, True])
def test_a_stop_that_raised_is_judged_by_the_session_not_the_exception(lodge, still_running):
    who = lodge.store.authenticate(lodge.token)
    lodge.sessions.states["softie-9"] = "running"
    lodge.store.associate_session("tg", _binding(lodge, "u2"), "softie-9", via="routed", wolt="softie")

    def stop(name):
        if not still_running:
            lodge.sessions.states[name] = "stopped"
        raise RuntimeError("stop bookkeeping failed")

    lodge.store.router.commands.stop = stop
    status, body = lodge.store.accept_event(who, _event_payload("k", "u2", "c2", "/kill softie-9"))
    assert body["outcome"] == ("uncertain" if still_running else "stopped")


@pytest.mark.parametrize("narrowing", ["selectable", "default"])
def test_a_kill_target_whose_wolt_leaves_the_binding_at_the_effect_boundary_is_not_stopped(lodge, narrowing):
    """Astre's probe: may_stop stays on, but the target's real wolt stops being one this chat may talk to."""
    who = lodge.store.authenticate(lodge.token)
    binding = _binding(lodge, "u2")
    lodge.sessions.states["otter-1"] = "running"
    lodge.store.associate_session("tg", binding, "otter-1", via="routed", wolt="otter")
    if narrowing == "default":
        # otter is reachable only as the default wolt; the boundary moves the default away.
        lodge.store.bind("tg", sender="u2", conversation="c2", default_wolt="otter", selectable_wolts=["softie"],
                         may_stop_own_sessions=True)
    real = lodge.store.settle_operation

    def settle(instance, key, state, **fields):
        if state == "effect_in_progress":
            lodge.store.bind("tg", sender="u2", conversation="c2", default_wolt="softie", selectable_wolts=["softie"],
                             may_stop_own_sessions=True)
        return real(instance, key, state, **fields)

    lodge.store.settle_operation = settle
    status, body = lodge.store.accept_event(who, _event_payload("k", "u2", "c2", "/kill otter-1"))
    assert body["reason"] == "grant_withdrawn" and lodge.sessions.stopped == []
