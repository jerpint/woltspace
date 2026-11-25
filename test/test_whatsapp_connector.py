"""Offline WhatsApp folder connector: no provider credentials, network or models."""
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT / "container/connectors/whatsapp"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "container/lib"))
sys.path.insert(0, str(FOLDER))
import provider
REAL_API = provider.api
import whatsapp_connector as wa
import connector_registry
from connector_contract import Choice, Reply
from connector_protocol import reply_to_wire
from woltspace.channels import FolderConnector
from woltspace.layout import RuntimeLayout


@pytest.fixture
def app(tmp_path, monkeypatch):
    instance = wa.WhatsAppConnector("synthetic-key", tmp_path / "data", ["user:1"])
    instance.client.event = AsyncMock(return_value="accepted")
    instance.commands = ["help", "wolt", "sessions", "new"]
    monkeypatch.setattr(provider, "api", lambda *args: (200, {}))
    return instance


def bind(app):
    provider.atomic_json(provider._owner_path(), {"account": app.account, "participant": app.owner})


def message(**kw):
    return {"id": "message/id/1", "from": "user:1", "timestamp": "100", "type": "text",
            "text": {"body": "hello"}, **kw}


def batch(*items, offset=2):
    return {"next_offset": offset, "entry": [{"changes": [{"value": {"messages": list(items)}}]}]}


def poll(app, *items, state=None):
    dispatched = []
    state = state if state is not None else {"offset": 0, "processed": []}
    provider.poll_once(state, 100, request=lambda *a: (200, batch(*items)), dispatch=dispatched.append)
    return dispatched, state


def test_manifest_discovered_without_core_registration(tmp_path):
    spec = connector_registry.load_folder(FOLDER, bundled=True)
    assert spec.name == "whatsapp"
    assert spec.manifest.capabilities == {"text"}
    assert spec.manifest.secrets == ("WHATSAPP_AGENT_TOKEN",)
    layout = RuntimeLayout(tmp_path / "wolts", ROOT, "127.0.0.1", 7798, "host")
    channel = FolderConnector(spec)
    assert not channel.plan(layout, {}).enabled
    config = {"channels": {"whatsapp": {"enabled": True, "agent_token": "synthetic", "owners": ["user:1"]}}}
    # Native configuration is loaded through the layout, not the caller's env.
    p = layout.wolts_dir / ".space/platform/config.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps(config))
    plan = channel.plan(layout, {})
    assert plan.enabled
    assert "synthetic" not in json.dumps(plan.to_record())
    assert plan.env["WOLTSPACE_CONNECTOR_OWNERS"] == "user:1"
    assert plan.command[-1] == "whatsapp_connector.py"


@pytest.mark.parametrize("owners", [[], ["user:1", "user:2"], ["garbage"]])
def test_requires_explicit_single_creator(tmp_path, owners):
    with pytest.raises(provider.WhatsAppConfigurationError):
        wa.WhatsAppConnector("synthetic", tmp_path, owners)


def test_route_round_trips_and_account_rotation_separates_sessions(app):
    spec = connector_registry.load_folder(FOLDER, bundled=True)
    text = spec.codec.format(app.route)
    assert spec.codec.canonical(text) == app.route
    assert spec.codec.matches(app.route, {"adapter": "whatsapp", **app.route})
    old = dict(app.route)
    other = wa.WhatsAppConnector("rotated-synthetic", provider.STATE, ["user:1"])
    assert other.route != old
    assert not other.valid_route(old)
    assert not provider._owner_path().exists()


def test_startup_rejects_conflicting_creator_before_polling(app, monkeypatch):
    bind(app)
    binding = provider._owner_path()
    original_binding = binding.read_bytes()
    cursor = provider.CURSOR
    provider.atomic_json(cursor, {"offset": 7})
    original_cursor = cursor.read_bytes()
    monkeypatch.setenv("WHATSAPP_AGENT_TOKEN", "synthetic-key")
    monkeypatch.setenv("WOLTSPACE_CONNECTOR_OWNERS", "user:different")
    monkeypatch.setenv("WOLTSPACE_CONNECTOR_DATA", str(provider.STATE))
    run = AsyncMock()
    monkeypatch.setattr(wa.WhatsAppConnector, "run", run)

    with pytest.raises(SystemExit, match="use the bound creator or a fresh key"):
        wa.main()

    run.assert_not_called()
    assert binding.read_bytes() == original_binding
    assert cursor.read_bytes() == original_cursor


def test_first_authenticated_text_pins_before_dispatch_and_checkpoint(app):
    seen = []
    def dispatch(item):
        seen.append(item)
        assert provider._creator() == app.owner
        assert "message/id/1" in provider.load(provider.CURSOR)["processed"]
        assert provider.load(provider.CURSOR)["offset"] == 2
    provider.poll_once({"offset": 0}, 100, request=lambda *a: (200, batch(message())), dispatch=dispatch)
    assert seen == [message()]
    assert provider._owner_path().stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("item", [message(**{"from": "user:2"}), message(timestamp="99"),
    message(type="image"), message(text={"body": ""}), message(text={"body": "x" * 4097}),
    message(id=""), message(timestamp="oops")])
def test_invalid_or_foreign_first_item_never_enrolls(app, item):
    dispatched, _ = poll(app, item)
    assert dispatched == []
    assert not provider._owner_path().exists()


def test_pin_failure_prevents_dispatch_and_replay(app, monkeypatch):
    original = provider.atomic_json
    def fail_owner(path, value):
        if path == provider._owner_path():
            raise OSError("synthetic")
        original(path, value)
    monkeypatch.setattr(provider, "atomic_json", fail_owner)
    with pytest.raises(provider.WhatsAppConfigurationError):
        poll(app, message())
    assert "message/id/1" in provider.load(provider.CURSOR)["processed"]


def test_duplicate_and_old_item_are_checkpointed_once(app):
    dispatched, state = poll(app, message(), message(), message(id="old", timestamp="99"))
    assert len(dispatched) == 1
    assert len(state["processed"]) == 2
    assert poll(app, message(), state=state)[0] == []


def test_204_and_malformed_batch_preserve_offset(app):
    state = {"offset": 7}
    assert provider.poll_once(state, 100, request=lambda *a: (204, {}), dispatch=lambda i: None) == state
    assert not provider.CURSOR.exists()
    with pytest.raises(ValueError):
        provider.poll_once(state, 100, request=lambda *a: (200, batch(offset=6)), dispatch=lambda i: None)
    assert state == {"offset": 7}


@pytest.mark.parametrize("kind", ["symlink", "fifo", "foreign", "mode"])
def test_invalid_owner_binding_fails_closed(app, tmp_path, kind):
    path = provider._owner_path()
    if kind == "symlink":
        target = tmp_path / "target"
        target.write_text("{}")
        path.symlink_to(target)
    elif kind == "fifo":
        __import__("os").mkfifo(path)
    else:
        provider.atomic_json(path, {"account": "wrong" if kind == "foreign" else app.account, "participant": app.owner})
        if kind == "mode":
            path.chmod(0o644)
    with pytest.raises(provider.WhatsAppConfigurationError):
        provider._creator()


@pytest.mark.parametrize("text,kind,body", [
    ("/wolt impa", "command", {"name": "wolt", "args": ["impa"]}),
    ("/new", "command", {"name": "new", "args": []}),
    ("/compact", "message", {"text": "/compact", "mention": False}),
    (" /wolt", "message", {"text": " /wolt", "mention": False}),
    ("//wolt", "message", {"text": "//wolt", "mention": False}),
    ("@dog help", "message", {"text": "@dog help", "mention": True}),
])
def test_translates_using_lodge_menu_and_dog_flag(app, text, kind, body):
    bind(app)
    asyncio.run(app.dispatch(message(text={"body": text})))
    args = app.client.event.call_args
    assert args.args == (kind, app.route, app.owner, body)
    assert args.kwargs["event_id"].startswith("wa-" + app.account)
    assert len(args.kwargs["event_id"]) <= 128


def test_event_id_is_stable_and_namespaced(app):
    bind(app)
    asyncio.run(app.dispatch(message()))
    first = app.client.event.call_args.kwargs["event_id"]
    asyncio.run(app.dispatch(message()))
    assert app.client.event.call_args.kwargs["event_id"] == first


def test_sends_only_bound_text_and_refuses_other_routes(app, monkeypatch):
    calls = []
    monkeypatch.setattr(provider, "api", lambda *args: (calls.append(args) or (200, {})))
    async def run():
        assert (await app.on_send({"method": "message", "route": app.route, "text": "hello"}))["status"] == "refused"
        bind(app)
        assert (await app.on_send({"method": "message", "route": app.route, "text": "hello"}))["status"] == "delivered"
        for route in ({"chat_id": "0" * 20 + app.route["chat_id"][20:]}, {"chat_id": app.account + ".dXNlcjoy"}):
            assert (await app.send_text(route, "hello"))["status"] == "refused"
        for method in ("document", "voice", "audio"):
            assert (await app.on_send({"method": method, "route": app.route, "text": "hello"}))["status"] == "refused"
        for text in ("", "x" * 4097):
            assert (await app.send_text(app.route, text))["status"] == "refused"
    asyncio.run(run())
    assert len(calls) == 1
    assert calls[0][2]["to"] == "user:1"


@pytest.mark.parametrize("error,status", [(TimeoutError("secret private body"), "ambiguous"),
    (provider.WhatsAppAPIError("secret", http_status=503), "ambiguous"),
    (provider.WhatsAppAPIError("secret", http_status=400), "failed"),
    (provider.WhatsAppConfigurationError("secret"), "failed")])
def test_no_retry_or_error_body_leak_on_unconfirmed_send(app, monkeypatch, error, status):
    bind(app)
    calls = []
    def fail(*args):
        calls.append(args)
        raise error
    monkeypatch.setattr(provider, "api", fail)
    result = asyncio.run(app.send_text(app.route, "hello"))
    assert result["status"] == status
    assert "secret" not in json.dumps(result)
    assert len(calls) == 1


def test_numbered_picker_is_delivered_then_consumed_once(app):
    bind(app)
    reply = Reply("Pick a wolt", ((Choice("impa", action="wolt:impa"),),))
    async def run():
        await app.on_say({"route": app.route, "reply": reply_to_wire(reply)})
        await app.dispatch(message(text={"body": "1"}))
        assert app.client.event.call_args.args[-1] == {"action": "wolt:impa"}
        assert app.client.event.call_args.args[0] == "selection"
        await app.dispatch(message(id="m2", text={"body": "1"}))
        assert app.client.event.call_args.args[0] == "message"
    asyncio.run(run())


def test_failed_or_expired_picker_never_runs_selection(app, monkeypatch):
    bind(app)
    reply = Reply("Pick", ((Choice("impa", action="wolt:impa"),),))
    async def run():
        app.send_text = AsyncMock(return_value={"status": "ambiguous"})
        await app.on_say({"route": app.route, "reply": reply_to_wire(reply)})
        assert not app.choices
        app.send_text = AsyncMock(return_value={"status": "delivered"})
        await app.on_say({"route": app.route, "reply": reply_to_wire(reply)})
        app.choices_until = 0
        await app.dispatch(message(text={"body": "1"}))
        assert app.client.event.call_args.args[0] == "message"
    asyncio.run(run())


def test_only_explicit_429_is_retried_and_budget_shared(app, monkeypatch):
    attempts, sleeps = [], []
    def request(*args):
        attempts.append(args)
        if len(attempts) < 3:
            raise provider.WhatsAppRateLimit(2)
        return 200, {}
    monkeypatch.setattr(provider, "_api_request", request)
    monkeypatch.setattr(provider.time, "sleep", sleeps.append)
    # Fixture replaces api to keep every other test strictly offline.
    REAL_API("POST", "/messages", {})
    assert len(attempts) == 3
    assert sleeps == [2, 2]



def test_socket_event_receipt_dedup_picker_edit_and_notify(app, monkeypatch):
    """Real socket, real wire, real broker; provider HTTP alone is mocked."""
    import shutil
    import tempfile
    import connector_broker
    import connector_client
    bind(app)
    sent = []
    monkeypatch.setattr(provider, "api", lambda *args: (sent.append(args) or (200, {})))
    async def run():
        seen, events = set(), []
        def dedup(name, event_id):
            fresh = event_id not in seen
            seen.add(event_id)
            return fresh
        async def handler(event, out):
            events.append(event)
            if event["kind"] == "selection":
                from connector_contract import Ack
                await out.ack(Ack("Selected"))
                await out.edit(Reply("Selection done"))
            else:
                await out.say(Reply("Pick", ((Choice("impa", action="wolt:impa"),),)))
        # Keep unix socket paths short and inside this checkout, even if pytest
        # uses a long temporary directory. No live lodge socket is contacted.
        (ROOT / ".tmp").mkdir(exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix="s", dir=ROOT / ".tmp"))
        broker = connector_broker.Broker(directory, dedup)
        broker.add("whatsapp", handler, [("wolt", "Choose a wolt")])
        await broker.start()
        client = connector_client.Client("whatsapp", socket_path=str(connector_broker.socket_path(directory, "whatsapp")),
            token=connector_broker.new_token(directory, "whatsapp"), on_say=app.on_say,
            on_edit=app.on_edit, on_ack=app.on_ack, on_send=app.on_send)
        app.client = client
        try:
            await client.connect()
            await app.dispatch(message())
            await broker.drain("whatsapp")
            await client.ping()
            assert app.choices
            await app.dispatch(message(id="selection", text={"body": "1"}))
            await broker.drain("whatsapp")
            await client.ping()
            assert events[-1]["kind"] == "selection"
            assert [c[2]["text"]["body"] for c in sent][-2:] == ["Selected", "Selection done"]
            await app.dispatch(message())
            await broker.drain("whatsapp")
            assert len(events) == 2  # same provider id receives duplicate receipt
            result = await broker.send("whatsapp", app.route, "message", "session reply")
            assert result["status"] == "delivered"
            assert sent[-1][2]["to"] == app.owner
            # Exercise the generic server notification path as well as the
            # broker: WhatsApp has no custom server sender or CLI branch.
            from server import notify
            from unittest.mock import Mock
            monkeypatch.setattr(connector_broker, "_BROKER", broker)
            monkeypatch.setattr(notify, "WOLTS_DIR", provider.STATE.parent / "wolts")
            monkeypatch.setattr(notify, "SPACE_PLATFORM_DIR", provider.STATE / "platform")
            monkeypatch.setattr(notify, "read_session_registry", lambda name: None)
            monkeypatch.setattr(notify, "append_chat_history", Mock())
            route_text = connector_registry.load_folder(FOLDER, bundled=True).codec.format(app.route)
            result = await notify.send_notification("", "generic notify", {"adapter": "whatsapp", "route": route_text})
            assert result["adapter"] == "whatsapp"
            assert sent[-1][2]["text"]["body"] == "generic notify"
            with pytest.raises(connector_broker.SendFailed) as refused:
                await broker.send("whatsapp", {"chat_id": "0" * 20 + app.route["chat_id"][20:]}, "message", "wrong")
            assert refused.value.status == "refused"
        finally:
            await client.close()
            await broker.stop()
            shutil.rmtree(directory)
    asyncio.run(run())


def test_real_lodge_commands_and_owner_check_without_models(app, monkeypatch, tmp_path):
    sys.path.insert(0, str(ROOT / "container"))
    sys.path.insert(0, str(ROOT / "test"))
    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(tmp_path / "wolts"))
    import chat_lodge
    import connector_loopback
    import lodge_commands
    bind(app)
    spec = connector_registry.load_folder(FOLDER, bundled=True)
    lodge = chat_lodge.ChatLodge(spec, [app.owner], help_tips=spec.help)
    (lodge.wolts_dir / "impa/wolt").mkdir(parents=True)
    monkeypatch.setattr(chat_lodge, "get_tunnel_url", lambda: "https://lodge.example")
    monkeypatch.setattr(chat_lodge, "list_wolts", lambda: [{"name": "impa", "type": "otter"}])
    monkeypatch.setattr(lodge, "_wolt_exists", lambda name: name == "impa")
    monkeypatch.setattr(lodge, "_shown", lambda name: name)
    spawns = []
    monkeypatch.setattr(lodge, "_spawn_session", lambda *args: spawns.append(args) or {"name": "impa-session", "url": ""})
    app.client = connector_loopback.Loopback(lodge, app)
    app.commands = lodge_commands.COMMAND_NAMES
    app.send_text = AsyncMock(return_value={"status": "delivered"})
    async def run():
        await app.dispatch(message(text={"body": "/help"}))
        assert "/wolt" in app.send_text.call_args.args[1]
        assert spawns == []
        await app.dispatch(message(id="w", text={"body": "/wolt impa"}))
        assert lodge._load_chat_state(lodge.chat(app.route))["active_wolt"] == "impa"
        await app.dispatch(message(id="n", text={"body": "/new impa"}))
        assert len(spawns) == 1
        app.send_text.reset_mock()
        await app.client.event("command", app.route, "user:foreign", {"name": "new", "args": ["impa"]})
        assert len(spawns) == 1
        app.send_text.assert_not_called()
    asyncio.run(run())


def test_one_dispatch_failure_does_not_discard_rest_of_checkpointed_batch(app, monkeypatch):
    calls, dispatched = [], []
    def fetch(state, activation, *, dispatch):
        calls.append(1)
        if len(calls) > 1:
            raise provider.WhatsAppConfigurationError("synthetic stop")
        dispatch(message(id="bad"))
        dispatch(message(id="good"))
        return state
    async def deliver(item):
        dispatched.append(item["id"])
        if item["id"] == "bad":
            raise ValueError("synthetic malformed socket frame")
    monkeypatch.setattr(provider, "poll_once", fetch)
    app.dispatch = deliver
    with pytest.raises(provider.WhatsAppConfigurationError):
        asyncio.run(app.poll())
    assert dispatched == ["bad", "good"]


def test_a_notification_closes_an_older_picker(app):
    bind(app)
    app.choices = [Choice("Stop session", action="sstop:old")]
    app.choices_until = float("inf")
    async def run():
        assert (await app.on_send({"method": "message", "route": app.route, "text": "New session update"}))["status"] == "delivered"
        await app.dispatch(message(text={"body": "1"}))
        assert app.client.event.call_args.args[0] == "message"
    asyncio.run(run())


@pytest.mark.parametrize("owner", ["user:x", "user:x:y", "user:" + "x" * 123])
def test_chat_id_survives_session_record_and_rejects_old_route(tmp_path, owner):
    instance = wa.WhatsAppConnector("synthetic-key", tmp_path, [owner])
    spec = connector_registry.load_folder(FOLDER, bundled=True)
    from sessions import SessionRegistry
    record = SessionRegistry(tmp_path).create(
        "impa-route-test", wolt="impa", adapter="whatsapp", **instance.route)
    assert spec.codec.from_record(record) == instance.route
    assert record["routing"] == [{"adapter": "whatsapp", **instance.route}]
    assert spec.codec.matches(instance.route, record)
    with pytest.raises(ValueError):
        spec.codec.canonical(instance.route["chat_id"].replace(".", ":"))


def test_picker_survives_followup_for_same_event_and_clears_for_another(app):
    bind(app)
    async def run():
        picker = {"type": "say", "event": "first", "route": app.route,
                  "reply": reply_to_wire(Reply("Pick", ((Choice("impa", action="hold:impa:g1"),),)))}
        await app.on_say(picker)
        deadline = app.choices_until
        await app.on_say({**picker, "reply": reply_to_wire(Reply("Your message is held"))})
        assert app.choices[0].action == "hold:impa:g1"
        assert app.choices_until == deadline
        await app.on_say({**picker, "event": "other", "reply": reply_to_wire(Reply("Done"))})
        assert not app.choices
    asyncio.run(run())


@pytest.mark.parametrize("inbox_on", [False, True])
@pytest.mark.parametrize("selection", ["number", "command_after_expiry"])
def test_first_message_picker_session_and_notify_over_socket(
        app, monkeypatch, tmp_path, fake_runtime, inbox_on, selection):
    import shutil
    import tempfile
    import connector_broker
    import connector_client
    import connector_events
    import chat_lodge
    import inbox
    import inbox_wake
    import paths
    import sessions
    from inbox_support import rows
    from server import notify
    from unittest.mock import Mock

    root = tmp_path / "wolts"
    for name in ("impa", "beta"):
        home = root / name / "wolt"
        home.mkdir(parents=True)
        (home / "wolt.json").write_text('{"type": "otter"}')
    (root / "woltspace.json").write_text(json.dumps({"inbox": {"enabled": inbox_on}, "conversation_log": {"enabled": inbox_on}}))
    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(root))
    monkeypatch.setenv("WOLTSPACE_ISOLATION", "external")
    for module in (sessions, paths, notify):
        monkeypatch.setattr(module, "WOLTS_DIR", root)
    monkeypatch.setattr(sessions, "ensure_site", lambda *a: None)
    monkeypatch.setattr(sessions, "_schedule_pickup", lambda *a: None)
    monkeypatch.setattr(sessions, "get_tunnel_url", lambda: "https://lodge.example")
    monkeypatch.setattr(chat_lodge, "get_tunnel_url", lambda: "https://lodge.example")
    monkeypatch.setattr(chat_lodge, "dog_available", lambda: False)
    monkeypatch.setattr(chat_lodge, "_bot_log", lambda *a: None)
    monkeypatch.setattr(chat_lodge, "resolve_active_session", lambda *a: None)
    monkeypatch.setattr(chat_lodge, "list_wolts",
                        lambda: [{"name": n, "type": "otter"} for n in ("impa", "beta")])
    monkeypatch.setattr(chat_lodge, "start_claude_session",
                        lambda **kw: sessions.start_session(**kw))
    monkeypatch.setattr(chat_lodge, "notify_admitted", lambda *a: None)
    monkeypatch.setattr(notify, "append_chat_history", Mock())
    monkeypatch.setattr(notify, "_tunnel_url", lambda: "https://lodge.example")
    monkeypatch.setattr(notify, "SPACE_PLATFORM_DIR", root / ".space/platform")
    monkeypatch.setattr(notify, "OUTBOX_DIR", root / ".space/outbox")
    spec = connector_registry.load_folder(FOLDER, bundled=True)
    side = chat_lodge.ChatLodge(spec, [app.owner], help_tips=spec.help)
    bind(app)
    sent = []
    monkeypatch.setattr(provider, "api", lambda *a: (sent.append(a) or (200, {})))
    (ROOT / ".tmp").mkdir(exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="s", dir=ROOT / ".tmp"))

    async def run():
        broker = connector_broker.Broker(
            directory, lambda name, key: connector_events.record(root, name, key))
        broker.add("whatsapp", side.handle, [("help", "Help"), ("wolt", "Choose"), ("new", "New")])
        await broker.start()
        client = connector_client.Client(
            "whatsapp", socket_path=str(connector_broker.socket_path(directory, "whatsapp")),
            token=connector_broker.new_token(directory, "whatsapp"),
            on_say=app.on_say, on_edit=app.on_edit, on_ack=app.on_ack, on_send=app.on_send)
        app.client = client
        monkeypatch.setattr(connector_broker, "_BROKER", broker)
        try:
            welcome = await client.connect()
            app.commands = [c[0] for c in welcome["commands"]]
            first = message(text={"body": "Keep this first message"})
            await app.dispatch(first)
            await broker.drain("whatsapp")
            await client.ping()
            assert app.choices and app.choices[0].action.startswith("hold:impa:")
            assert "message is held" in sent[-1][2]["text"]["body"]
            if selection == "command_after_expiry":
                app.choices_until = 0
                await app.dispatch(message(id="expired-number", text={"body": "1"}))
                await broker.drain("whatsapp")
                await client.ping()
                assert not fake_runtime.spawns
                assert rows(root, "SELECT state FROM pending_messages") == [("pending",)]
            pick = message(id="pick", text={"body": "1" if selection == "number" else "/wolt impa"})
            await app.dispatch(pick)
            await broker.drain("whatsapp")
            await client.ping()
            await app.dispatch(pick)
            await broker.drain("whatsapp")
            await client.ping()
            assert rows(root, "SELECT state FROM pending_messages") == [("claimed",)]
            if inbox_on:
                assert rows(root, "SELECT count(*) FROM inbox_messages") == [(1,)]
                store = inbox.Store(root, inbox.LIMITS)
                facts = store.wake_facts("impa")
                assert facts["human"] == 1
                assert "--to whatsapp:" + app.route["chat_id"] in rows(
                    root, "SELECT body FROM inbox_messages")[0][0].decode()
                how, name = inbox_wake._start(store, sessions.SessionRegistry(root), "impa", facts, None)
                assert how == "started"
            else:
                assert len(fake_runtime.spawns) == 1
                name = fake_runtime.spawns[0][0]
            record = sessions.SessionRegistry(root).get(name, check_alive=False)
            assert spec.codec.from_record(record) == app.route
            assert "--to whatsapp:" + app.route["chat_id"] in sessions._adapter_context(record)
            assert len(fake_runtime.spawns) == 1
            for explicit in (None, {"adapter": "whatsapp", "route": app.route["chat_id"]}):
                result = await notify.send_notification(name, "Session reply", explicit)
                assert result["adapter"] == "whatsapp"
                text = sent[-1][2]["text"]["body"]
                assert text.startswith("Session reply") and "session=" + name in text
            if inbox_on:
                assert rows(root, "SELECT count(*) FROM conversation_log WHERE kind = 'notify' AND outcome = 'accepted'") == [(2,)]
            await client.event("command", app.route, "user:foreign",
                               {"name": "new", "args": ["impa"]}, event_id="foreign")
            await broker.drain("whatsapp")
            assert len(fake_runtime.spawns) == 1
            before = len(sent)
            with pytest.raises(RuntimeError, match="refused"):
                await notify.send_notification(name, "x" * wa.MAX_TEXT, None)
            assert len(sent) == before
            old = app.route.copy()
            rotated = wa.WhatsAppConnector("rotated", provider.STATE, [app.owner])
            monkeypatch.setattr(app, "route", rotated.route)
            monkeypatch.setattr(app, "account", rotated.account)
            bind(app)
            assert old != app.route
            with pytest.raises(RuntimeError, match="refused"):
                await notify.send_notification(name, "Old account", None)
            assert len(sent) == before
            assert not app.choices
        finally:
            await client.close()
            await broker.stop()
    try:
        asyncio.run(run())
    finally:
        shutil.rmtree(directory)


def test_failed_receipt_is_reported_without_redispatch(app, caplog):
    bind(app)
    app.client.event = AsyncMock(return_value="failed")
    asyncio.run(app.dispatch(message()))
    assert app.client.event.await_count == 1
    assert "checkpointed WhatsApp event" in caplog.text


def test_supervised_connector_process_with_fake_provider(monkeypatch):
    """Run the actual connector entry point in a child, with provider I/O replaced."""
    import shutil
    import tempfile
    from unittest.mock import Mock
    import connector_broker
    from woltspace import channel_supervisor
    from server import notify

    directory = Path(tempfile.mkdtemp(prefix="wa"))
    root = directory / "w"
    folder = root / ".space/connectors/whatsapp"
    folder.mkdir(parents=True)
    for source in FOLDER.iterdir():
        if source.suffix in (".py", ".json"):
            shutil.copy2(source, folder / source.name)
    manifest = json.loads((folder / "connector.json").read_text())
    manifest["command"] = ["{python}", "fake_process.py"]
    (folder / "connector.json").write_text(json.dumps(manifest))
    (folder / "fake_process.py").write_text("""
import json
import os
from pathlib import Path
import time
import provider
import whatsapp_connector

data = Path(os.environ["WOLTSPACE_CONNECTOR_DATA"])
data.mkdir(parents=True, exist_ok=True)
(data / "env.json").write_text(json.dumps(sorted(os.environ)))

def request(method, path, payload=None):
    if method == "POST":
        with (data / "sent.jsonl").open("a") as output:
            output.write(json.dumps(payload) + chr(10))
        return 200, {}
    time.sleep(0.05)
    item = {"id": "child-help", "from": "user:1", "timestamp": str(int(time.time())),
            "type": "text", "text": {"body": "/help"}}
    return 200, {"next_offset": 1, "entry": [{"changes": [{"value": {"messages": [item]}}]}]}

# poll_once also retains the original api in its default arguments.
provider.api = request
provider._api_request = request
def no_network(*args, **kwargs):
    raise AssertionError("Unexpected provider network call")
provider.urllib.request.urlopen = no_network
whatsapp_connector.main()
""")
    config = root / ".space/platform/config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"channels": {"whatsapp": {
        "enabled": True, "agent_token": "synthetic-child-key", "owners": ["user:1"]}}}))
    spec = connector_registry.load_folder(folder, bundled=False)
    layout = RuntimeLayout(root, ROOT, "127.0.0.1", 7798, "external")
    plan = FolderConnector(spec).plan(layout, {})
    assert plan.enabled, plan.detail
    real_environment = channel_supervisor.child_environment
    monkeypatch.setattr(channel_supervisor, "child_environment",
                        lambda p: real_environment(p, {"PATH": "/usr/bin:/bin", "PARENT_SECRET": "private"}))
    events = []

    def posts():
        path = root / ".space/whatsapp/sent.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    async def until(check):
        for _ in range(200):
            if check():
                return
            await asyncio.sleep(0.05)
        log = layout.logs_dir / "connector-whatsapp.log"
        raise AssertionError(log.read_text() if log.exists() else "The isolated connector did not start")

    async def run():
        async def handle(event, out):
            events.append(event)
            await out.say(Reply("Lodge help"))
        seen = set()
        def fresh(name, key):
            if key in seen:
                return False
            seen.add(key)
            return True
        broker = connector_broker.Broker(Path(plan.socket_dir), fresh)
        broker.add("whatsapp", handle, [("help", "Help")])
        await broker.start()
        supervisor = channel_supervisor.ChannelSupervisor(layout, [plan], already_reaped=True)
        try:
            supervisor.start(watch=False)
            await until(lambda: len(posts()) == 1)
            assert events[0]["kind"] == "command" and events[0]["sender"] == "user:1"
            assert events[0]["body"] == {"name": "help", "args": []}
            route = events[0]["route"]["chat_id"]
            assert spec.codec.canonical(route) == events[0]["route"]
            monkeypatch.setattr(connector_broker, "_BROKER", broker)
            monkeypatch.setattr(notify, "WOLTS_DIR", root)
            monkeypatch.setattr(notify, "OUTBOX_DIR", root / ".space/outbox")
            monkeypatch.setattr(notify, "append_chat_history", Mock())
            await notify.send_notification("", "Child notify", {"adapter": "whatsapp", "route": route})
            await until(lambda: len(posts()) == 2)
            assert [post["text"]["body"] for post in posts()] == ["Lodge help", "Child notify"]
            names = json.loads((root / ".space/whatsapp/env.json").read_text())
            assert "WHATSAPP_AGENT_TOKEN" in names and "WOLTSPACE_CONNECTOR_TOKEN" in names
            assert "PARENT_SECRET" not in names
        finally:
            supervisor.stop(timeout=5)
            await broker.stop()
    try:
        asyncio.run(run())
    finally:
        shutil.rmtree(directory)
