"""A connector from outside the repository: one folder in the data root, no core edits.

"echo" lives only in this test's `<data root>/.space/connectors/echo/`. The
lodge finds it, plans it, starts its program as its own process with only its
own secrets, talks to it over its socket, routes `notify --to echo:<room>` to
it, and never imports it. Bad folders are skipped, and a local folder can
never replace a connector the lodge ships.
"""

import asyncio
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "container" / "lib"))
sys.path.insert(0, str(ROOT))

import connector_broker  # noqa: E402
import connector_registry  # noqa: E402
from connector_contract import Reply  # noqa: E402
from woltspace.channels import FolderConnector, TelegramConnector, all_connectors  # noqa: E402
from woltspace.layout import RuntimeLayout  # noqa: E402

MANIFEST = {
    "name": "echo",
    "title": "Echo",
    "protocol": 1,
    "command": ["{python}", "echo_connector.py"],
    "capabilities": ["text", "files"],
    "command_input": ["text"],
    "events": ["message", "command"],
    "route": [["room", "[a-z0-9]+"]],
    "secrets": [["ECHO_KEY", "key"]],
    # A local folder cannot use these: they are for connectors the lodge ships.
    "enable_env": "ENABLE_ECHO",
    "owners_env": "ECHO_OWNERS",
    "legacy_flag": "--echo",
}

PROGRAM = '''
import asyncio, json, os
from pathlib import Path
import connector_client

DATA = Path(os.environ["WOLTSPACE_CONNECTOR_DATA"])
DATA.mkdir(parents=True, exist_ok=True)

def log(entry):
    with open(DATA / "log.jsonl", "a") as f:
        f.write(json.dumps(entry) + "\\n")

async def on_say(frame):
    log({"say": frame})

async def on_send(frame):
    log({"send": {k: frame.get(k) for k in ("route", "method", "text", "filename")}})
    return {"status": "delivered", "message_id": "1"}

async def main():
    log({"env": sorted(os.environ)})
    client = connector_client.Client("echo", on_say=on_say, on_send=on_send)
    while True:
        try:
            await client.connect()
            break
        except OSError:
            await asyncio.sleep(0.1)
    status = await client.event("message", {"room": "r1"}, "owner-1", {"text": "hello"}, "echo-1")
    log({"receipt": status})
    await client.run_forever()

asyncio.run(main())
'''


@pytest.fixture
def lodge():
    # Unix socket paths are short (104 bytes on macOS): not under pytest's tmp_path.
    root = Path(tempfile.mkdtemp(prefix="wse", dir="/tmp"))
    wolts = root / "w"
    folder = wolts / ".space" / "connectors" / "echo"
    folder.mkdir(parents=True)
    (folder / "connector.json").write_text(json.dumps(MANIFEST))
    (folder / "echo_connector.py").write_text(PROGRAM)
    config = wolts / ".space" / "platform" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"channels": {"echo": {
        "enabled": True, "key": "s3cret", "owners": ["owner-1"]}}}))
    layout = RuntimeLayout(wolts_dir=wolts, install_root=ROOT, host="127.0.0.1", port=7798,
                           isolation="host")
    yield layout
    shutil.rmtree(root, ignore_errors=True)


ENV = {"WOLTSPACE_ENTRYPOINT": "1", "PARENT_SECRET": "leak", "TELEGRAM_BOT_TOKEN": "1:tg"}


# --- found, declared, planned ------------------------------------------------------

def test_a_local_folder_is_found_with_what_it_declares(lodge):
    spec = connector_registry.spec("echo", lodge.wolts_dir)
    assert spec is not None and spec.launchable and not spec.bundled
    assert spec.manifest.route_fields == ("room",)
    assert spec.codec.canonical("r1") == {"room": "r1"}
    with pytest.raises(ValueError):
        spec.codec.canonical("R1:x")
    # What only a shipped connector may have is not taken from a local folder.
    assert spec.enable_env == spec.owners_env == ""
    assert spec.reply_args({"room": "r1"}) == ["--to", "echo:r1"]
    assert "echo" in [c.name for c in all_connectors(lodge)]


def test_the_plan_gives_the_program_only_its_own_secrets_and_its_socket(lodge):
    plan = FolderConnector(connector_registry.spec("echo", lodge.wolts_dir)).plan(lodge, ENV)
    assert plan.enabled, plan.detail
    assert plan.isolated_env and plan.owners == ("owner-1",)
    assert plan.command == (sys.executable, "echo_connector.py")
    assert plan.cwd == str((lodge.wolts_dir / ".space" / "connectors" / "echo").resolve())
    assert plan.env["ECHO_KEY"] == "s3cret"
    assert plan.env["WOLTSPACE_CONNECTOR_SOCKET"].endswith("/connectors/echo.sock")
    assert "PARENT_SECRET" not in plan.env and "TELEGRAM_BOT_TOKEN" not in plan.env
    assert "ECHO_OWNERS" not in plan.env


def test_a_local_folder_is_off_until_its_config_says_on(lodge):
    config = lodge.wolts_dir / ".space" / "platform" / "config.json"
    config.write_text(json.dumps({"channels": {"echo": {"key": "s3cret"}}}))
    spec = connector_registry.spec("echo", lodge.wolts_dir)
    # Its manifest's enable_env is ignored: the environment cannot turn it on.
    assert not FolderConnector(spec).plan(lodge, {**ENV, "ENABLE_ECHO": "1"}).enabled


def test_a_local_folder_cannot_replace_the_bundled_telegram(lodge):
    fake = lodge.wolts_dir / ".space" / "connectors" / "telegram"
    fake.mkdir()
    (fake / "connector.json").write_text(json.dumps({**MANIFEST, "name": "telegram"}))
    spec = connector_registry.spec("telegram", lodge.wolts_dir)
    assert spec.bundled and spec.folder == str(ROOT / "container" / "connectors" / "telegram")
    assert TelegramConnector().spec.folder == spec.folder


@pytest.mark.parametrize("change", [
    {"name": "other"},                       # must match its folder
    {"protocol": 2},                         # unknown versions fail closed
    {"route": [["room", "("]]},              # a pattern that does not compile
    {"route": [["adapter", ".+"]]},          # a field the session record already uses
    {"command": []},
    {"capabilities": ["telepathy"]},
    {"secrets": [["lower_case", "k"]]},
])
def test_an_unusable_folder_is_skipped(lodge, change):
    path = lodge.wolts_dir / ".space" / "connectors" / "echo" / "connector.json"
    path.write_text(json.dumps({**MANIFEST, **change}))
    assert connector_registry.spec("echo", lodge.wolts_dir) is None
    assert "echo" not in [c.name for c in all_connectors(lodge)]


# --- launched, connected, routed ---------------------------------------------------

def _log(lodge) -> list[dict]:
    path = lodge.wolts_dir / ".space" / "echo" / "log.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


async def _until(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("timed out")


def test_the_program_runs_connects_and_carries_both_ways(lodge, monkeypatch):
    from woltspace import channel_supervisor

    plan = FolderConnector(connector_registry.spec("echo", lodge.wolts_dir)).plan(lodge, ENV)
    real_child_environment = channel_supervisor.child_environment
    # The control plane's own environment holds other secrets; the child must not see them.
    monkeypatch.setattr(channel_supervisor, "child_environment",
                        lambda p: real_child_environment(p, {**ENV, "PATH": "/usr/bin:/bin"}))
    events = []

    async def handle(event, out):
        events.append(event)
        await out.say(Reply("pong"))

    async def main():
        run_dir = Path(plan.socket_dir)
        seen = set()

        def dedup(name, event_id):
            fresh = (name, event_id) not in seen
            seen.add((name, event_id))
            return fresh

        broker = connector_broker.Broker(run_dir, dedup)
        broker.add("echo", handle)
        await broker.start()
        supervisor = channel_supervisor.ChannelSupervisor(lodge, [plan], already_reaped=True)
        supervisor.start(watch=False)
        try:
            await _until(lambda: any("receipt" in e for e in _log(lodge)))
            assert {"receipt": "accepted"} in _log(lodge)
            assert events[0]["route"] == {"room": "r1"} and events[0]["body"]["text"] == "hello"
            await _until(lambda: any("say" in e for e in _log(lodge)))

            # Notify, through the lodge's own outbound path, by the connector's route.
            from server import notify
            monkeypatch.setattr(connector_broker, "_BROKER", broker)
            monkeypatch.setattr(notify, "WOLTS_DIR", lodge.wolts_dir)
            monkeypatch.setattr(notify, "SPACE_PLATFORM_DIR", lodge.wolts_dir / ".space" / "platform")
            monkeypatch.setattr(notify, "read_session_registry", lambda name: None)
            monkeypatch.setattr(notify, "append_chat_history", Mock())
            result = await notify.send_notification("", "hi from a wolt\n", {"adapter": "echo", "route": "r1"})
            assert result["adapter"] == "echo"
            await _until(lambda: any("send" in e for e in _log(lodge)))
            [sent] = [e["send"] for e in _log(lodge) if "send" in e]
            assert sent == {"route": {"room": "r1"}, "method": "message",
                            "text": "hi from a wolt\n", "filename": None}

            # The program saw its own secret and socket, and nothing of the lodge's.
            env = next(e["env"] for e in _log(lodge) if "env" in e)
            assert "ECHO_KEY" in env and "WOLTSPACE_CONNECTOR_TOKEN" in env
            assert "PARENT_SECRET" not in env and "TELEGRAM_BOT_TOKEN" not in env

            # The lodge restarts: the program reconnects by itself and still carries.
            await broker.stop()
            broker = connector_broker.Broker(run_dir, dedup)
            broker.add("echo", handle)
            await broker.start()
            monkeypatch.setattr(connector_broker, "_BROKER", broker)
            await _until(lambda: broker.connected("echo"))
            await broker.send("echo", {"room": "r1"}, "message", "after the restart")
            await _until(lambda: len([e for e in _log(lodge) if "send" in e]) == 2)
        finally:
            supervisor.stop(timeout=5)
            await broker.stop()

    asyncio.run(main())
