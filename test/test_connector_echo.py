"""A connector the lodge has never seen, declared only in the registry (RFC #617).

"echo" exists nowhere in the core: no class in channels.py, no entry in
server/notify.py, no flag in the notify CLI, no branch in the session boot
text. These tests add its spec to the registry and nothing else, then check
that the lodge can start it, route a wolt's reply to it, bind it to a session,
and tell a session how to answer on it. That is the claim of the contract:
one connector, no core edits.
"""

import asyncio
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "container" / "lib"))

import connector_registry  # noqa: E402
from connector_contract import ConnectorSpec, Manifest, RouteCodec  # noqa: E402
from server.notification_types import Plugin  # noqa: E402

ROOM = re.compile(r"[a-z0-9]{1,32}")
SENT = []


async def _send(message, destination, context):
    SENT.append({"text": message.text, "link": message.session_link,
                 "destination": dict(destination), "token": context.secrets.get("ECHO_TOKEN")})


# The connector's own sender, named by its spec as "<module>:ECHO_PLUGIN".
ECHO_PLUGIN = Plugin("echo", _send, secrets=("ECHO_TOKEN",))


def _parse(text):
    if not isinstance(text, str) or ROOM.fullmatch(text) is None:
        raise ValueError("an echo room is lowercase letters and digits")
    return {"room": text}


def _from_record(record):
    room = str(record.get("chat_id") or "")
    return {"room": room} if record.get("adapter") == "echo" and ROOM.fullmatch(room) else None


ECHO = ConnectorSpec(
    Manifest(name="echo", capabilities=frozenset({"text"}), command_input=frozenset({"text"}),
             route_fields=("room",), secrets=("ECHO_TOKEN",)),
    RouteCodec(("room",), _parse, _from_record),
    title="Echo",
    launch="bot.echo_adapter",
    outbound=f"{__name__}:ECHO_PLUGIN",
    config_keys=(("ECHO_TOKEN", "token"),),
    enable_env="ENABLE_ECHO",
)


@pytest.fixture(autouse=True)
def echo_registered(monkeypatch):
    monkeypatch.setitem(connector_registry.SPECS, "echo", ECHO)
    SENT.clear()
    yield


# --- launch ------------------------------------------------------------------

@pytest.fixture
def layout(tmp_path):
    from woltspace.layout import RuntimeLayout
    return RuntimeLayout(wolts_dir=tmp_path / "wolts", install_root=ROOT, port=7799)


def _config(layout, payload):
    layout.platform_state.mkdir(parents=True, exist_ok=True)
    (layout.platform_state / "config.json").write_text(json.dumps(payload))


def _echo_plan(layout, env):
    from woltspace.channels import plan_connectors
    return {p.name: p for p in plan_connectors(layout, env)}["echo"]


def test_the_launcher_plans_it_from_its_spec_alone(layout):
    _config(layout, {"channels": {"echo": {"enabled": True, "token": "s3cret"}}})
    plan = _echo_plan(layout, {"WOLTSPACE_ENTRYPOINT": "1", "TELEGRAM_BOT_TOKEN": "not-echo's"})
    assert plan.enabled
    assert plan.command[-2:] == ("-m", "bot.echo_adapter")
    assert plan.process_signature == ("-m", "bot.echo_adapter")
    # It receives its own declared secret, and no other connector's.
    assert plan.env["ECHO_TOKEN"] == "s3cret"
    assert "TELEGRAM_BOT_TOKEN" not in plan.env
    assert "s3cret" not in json.dumps(plan.to_record())


def test_it_stays_off_until_declared_and_needs_its_secret(layout):
    assert not _echo_plan(layout, {"WOLTSPACE_ENTRYPOINT": "1"}).enabled
    _config(layout, {"channels": {"echo": {"enabled": True}}})
    plan = _echo_plan(layout, {"WOLTSPACE_ENTRYPOINT": "1"})
    assert not plan.enabled and "ECHO_TOKEN" in plan.detail


def test_an_inherited_environment_never_starts_a_second_one(layout):
    plan = _echo_plan(layout, {"ENABLE_ECHO": "1", "ECHO_TOKEN": "x"})
    assert not plan.enabled and "entrypoint" in plan.detail


def test_its_secret_is_shared_with_the_lodge_only_when_enabled(layout):
    from woltspace.channels import connector_secrets, plan_connectors
    _config(layout, {"channels": {"echo": {"enabled": True, "token": "s3cret"}}})
    assert connector_secrets(plan_connectors(layout, {"WOLTSPACE_ENTRYPOINT": "1"}))["ECHO_TOKEN"] == "s3cret"


# --- the notify CLI ----------------------------------------------------------

def test_notify_to_names_it_and_its_codec_checks_the_route():
    from notify_prompt import validate_argv
    validate_argv(["notify", "--to", "echo:room1"])
    for bad in ("echo:Room1", "echo:room 1", "echo:", "nowhere:room1", "echo"):
        with pytest.raises(ValueError):
            validate_argv(["notify", "--to", bad])


def test_the_legacy_flags_still_work():
    from notify_prompt import validate_argv
    validate_argv(["notify", "--telegram", "-100123"])
    validate_argv(["notify", "--slack", "C123", "1712.5"])
    validate_argv(["notify", "--to", "telegram:-100123"])
    validate_argv(["notify", "--to", "slack:C123:1712.5"])


# --- the boot text -----------------------------------------------------------

def test_a_session_started_from_it_is_told_how_to_answer():
    from sessions import _adapter_context
    text = _adapter_context({"adapter": "echo", "chat_id": "room1", "session_url": "https://l/tui?session=s"})
    assert "This session was started from Echo." in text
    assert "notify --to echo:room1 <<'WOLTSPACE_NOTIFY_" in text
    # A record whose route the codec rejects gets no instruction at all.
    assert _adapter_context({"adapter": "echo", "chat_id": "room 1; rm -rf ~"}) == ""


def test_telegram_and_slack_boot_text_is_unchanged():
    from sessions import _adapter_context
    tg = _adapter_context({"adapter": "telegram", "chat_id": "42", "session_url": "u"})
    assert tg.startswith("\nThis session was started from Telegram.\n") and "notify --telegram 42 <<" in tg
    sl = _adapter_context({"adapter": "slack", "chat_id": "C1", "thread_ts": "1.2", "session_url": "u"})
    assert sl.startswith("\nThis session was started from Slack.\n") and "notify --slack C1 1.2 <<" in sl
    assert _adapter_context({"adapter": "slack", "chat_id": "C1"}) == ""


# --- outbound ----------------------------------------------------------------

@pytest.fixture
def notify(monkeypatch, tmp_path):
    from server import notify as module
    monkeypatch.setattr(module, "dotenv_env", lambda key: {"ECHO_TOKEN": "s3cret"}.get(key, ""))
    monkeypatch.setattr(module, "append_chat_history", lambda *a: None)
    monkeypatch.setattr(module, "SPACE_PLATFORM_DIR", tmp_path)
    monkeypatch.setattr(module, "OUTBOX_DIR", tmp_path / "outbox")
    records = {}
    monkeypatch.setattr(module, "read_session_registry", lambda name: records.get(name))
    return module, records


def test_a_wolt_reply_reaches_it_by_explicit_route(notify):
    module, _records = notify
    result = asyncio.run(module.send_notification("", "hi", explicit={"adapter": "echo", "route": "room1"}))
    assert result == {"adapter": "echo", "route": "room1"}
    assert SENT == [{"text": "hi", "link": None, "destination": {"room": "room1"}, "token": "s3cret"}]


def test_a_session_bound_to_it_replies_there_with_its_link(notify):
    module, records = notify
    records["s1"] = {"name": "s1", "wolt": "pal", "adapter": "echo", "chat_id": "room1"}
    asyncio.run(module.send_notification("s1", "done"))
    assert SENT[-1]["destination"] == {"room": "room1"}
    assert SENT[-1]["link"] == "session=s1"


def test_binding_is_exact(notify):
    """Another room is reachable by name, but not as the session's own route."""
    module, records = notify
    records["s1"] = {"name": "s1", "wolt": "pal", "adapter": "echo", "chat_id": "room1"}
    asyncio.run(module.send_notification("s1", "hi", explicit={"adapter": "echo", "route": "room2"}))
    assert SENT[-1]["destination"] == {"room": "room2"} and SENT[-1]["link"] is None


def test_a_malformed_route_is_refused(notify):
    module, _records = notify
    with pytest.raises(ValueError):
        asyncio.run(module.send_notification("", "hi", explicit={"adapter": "echo", "route": "../etc"}))
    assert SENT == []


def test_an_outside_connector_is_never_imported(notify, monkeypatch):
    """Only specs in the lodge's registry are resolved; a name it does not know
    has no sender, whatever a caller asks for."""
    module, _records = notify
    with pytest.raises(KeyError):
        module._sender("discord")


# --- the spawned process's real environment -----------------------------------

def _env_dumper(path):
    script = ("import json, os, sys, time; "
              f"open({str(path)!r}, 'w').write(json.dumps(dict(os.environ))); time.sleep(30)")
    return (sys.executable, "-c", script)


def _wait_for(path, timeout=10.0):
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists() and path.stat().st_size:
            return json.loads(path.read_text())
        time.sleep(0.05)
    raise AssertionError(f"{path} never written")


def test_the_spawned_process_gets_only_its_own_secret(layout, tmp_path, monkeypatch):
    """Checked on the real child, beside a built-in connector, with every
    connector's secret in the control plane's own environment."""
    import dataclasses
    from woltspace.channel_supervisor import ChannelSupervisor
    from woltspace.channels import ConnectorPlan
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-secret")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "slack-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    _config(layout, {"channels": {"echo": {"enabled": True, "token": "s3cret"}}})
    echo = dataclasses.replace(_echo_plan(layout, {"WOLTSPACE_ENTRYPOINT": "1"}),
                               command=_env_dumper(tmp_path / "echo.json"))
    builtin = ConnectorPlan("telegram", True, "built-in", _env_dumper(tmp_path / "telegram.json"),
                            str(ROOT), {"TELEGRAM_BOT_TOKEN": "telegram-secret"})
    sup = ChannelSupervisor(layout, [builtin, echo])
    sup.start(watch=False)
    try:
        echo_env = _wait_for(tmp_path / "echo.json")
        builtin_env = _wait_for(tmp_path / "telegram.json")
    finally:
        sup.stop()
    assert echo_env["ECHO_TOKEN"] == "s3cret"
    for secret in ("TELEGRAM_BOT_TOKEN", "SLACK_BOT_TOKEN", "OPENAI_API_KEY"):
        assert secret not in echo_env
    assert "PATH" in echo_env and echo_env["WOLTSPACE_API"]
    # A built-in connector with its own class inherits the environment, as before.
    assert builtin_env["TELEGRAM_BOT_TOKEN"] == "telegram-secret"


# --- files to its own route only ---------------------------------------------

def test_a_file_goes_to_the_sessions_own_route_and_nowhere_else(notify, monkeypatch, tmp_path):
    import dataclasses
    from server.notification_types import Attachment
    module, records = notify
    # A two-field route (room and thread), so the bound route is not just chat_id.
    def parse(text):
        room, _, thread = text.partition(":")
        if ROOM.fullmatch(room) is None or not thread.isdigit():
            raise ValueError("room:thread")
        return {"room": room, "thread": thread}

    def from_record(record):
        try:
            return parse(f"{record.get('chat_id')}:{record.get('thread_ts')}")
        except ValueError:
            return None

    file_echo = dataclasses.replace(
        ECHO,
        manifest=dataclasses.replace(ECHO.manifest, capabilities=frozenset({"text", "files"}),
                                     route_fields=("room", "thread")),
        codec=RouteCodec(("room", "thread"), parse, from_record),
        outbound=f"{__name__}:FILE_PLUGIN")
    monkeypatch.setitem(connector_registry.SPECS, "echo", file_echo)
    record = {"name": "s1", "wolt": "pal", "adapter": "echo", "chat_id": "room1", "thread_ts": "7"}
    path = tmp_path / "note.txt"
    path.write_text("hi")
    attachment = Attachment(path, "note.txt", 2, "text/plain")
    asyncio.run(module._send("echo", "s1", "here", "room1:7", routing=record,
                             attachments=(attachment,), session_record=record))
    assert SENT[-1]["destination"] == {"room": "room1", "thread": "7"}
    for other in ("room1:8", "room2:7"):
        with pytest.raises(module.AttachmentError):
            asyncio.run(module._send("echo", "", "here", other,
                                     attachments=(attachment,), session_record=record))


FILE_PLUGIN = Plugin("echo", _send, secrets=("ECHO_TOKEN",), max_attachment_bytes=1024)
