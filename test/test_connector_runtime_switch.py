"""`telegram.runtime`: exactly one runtime polls and one sends, the connector by default."""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "container" / "lib"))
sys.path.insert(0, str(ROOT / "connectors" / "fake"))

import server.app as server_app  # noqa: E402
from server import connector_api, notify  # noqa: E402
from server.connectors import (  # noqa: E402
    ConnectorError,
    ConnectorStore,
    Limits,
    channel_runtime,
    set_channel_runtime,
)
from server.notification_types import Plugin  # noqa: E402
from woltspace.channels import TelegramConnector  # noqa: E402
from woltspace.cli import main as cli_main  # noqa: E402
from woltspace.layout import RuntimeLayout  # noqa: E402
from woltspace_connector_fake import FakeConnector  # noqa: E402


@pytest.fixture
def colony(tmp_path, monkeypatch):
    wolts = tmp_path / "wolts"
    (wolts / ".space").mkdir(parents=True)
    for name in ("WOLTSPACE_WOLTS_DIR", "WOLTS_DIR"):
        monkeypatch.setenv(name, str(wolts))
    monkeypatch.setenv("WOLTSPACE_DIR", str(ROOT))
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    # These tests switch between the two runtimes: start on the old adapter,
    # which an owner chose explicitly.
    (wolts / "woltspace.json").write_text(json.dumps({"telegram": {"runtime": "adapter"}}))
    return wolts


@pytest.fixture
def store(colony, monkeypatch):
    store = ConnectorStore(colony / ".space" / "connectors", limits=Limits(outbound_file_max_bytes=1_000_000))
    store.add_instance("tg", "telegram", "bot:1")
    store.bind("tg", sender="u1", conversation="101", default_wolt="softie")
    store.enable("tg")
    monkeypatch.setattr(connector_api, "CONNECTORS_DIR", store.root)
    monkeypatch.setattr(connector_api, "_store", store)
    monkeypatch.setattr(connector_api, "_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(notify, "WOLTS_DIR", colony)
    return store


def _client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server_app.app, client=("127.0.0.1", 1)),
        base_url="http://localhost:7777",
    )


def test_the_connector_is_the_default_and_the_file_decides(colony):
    config = colony / "woltspace.json"
    config.unlink()
    assert channel_runtime(config, "telegram") == "connector"
    assert channel_runtime(None, "telegram") == "connector"
    assert channel_runtime(config, "fake") == "connector"
    config.write_text("{broken")
    assert channel_runtime(config, "telegram") == "connector"
    config.write_text(json.dumps({"default_harness": "claude"}))
    set_channel_runtime(config, "telegram", "connector")
    assert channel_runtime(config, "telegram") == "connector"
    assert json.loads(config.read_text())["default_harness"] == "claude"
    with pytest.raises(ConnectorError):
        set_channel_runtime(config, "telegram", "both")
    set_channel_runtime(config, "telegram", "adapter")
    assert channel_runtime(config, "telegram") == "adapter"


def test_the_store_derives_its_switch_from_the_lodge_layout(colony, store):
    assert store.runtime_config == colony / "woltspace.json"
    assert store.runtime_active("telegram") is False
    assert store.runtime_active("fake") is True
    set_channel_runtime(colony / "woltspace.json", "telegram", "connector")
    assert store.runtime_active("telegram") is True


async def test_under_the_adapter_the_connector_instance_hands_out_nothing(colony, store):
    """Nothing is leased, accepted or confirmed while the adapter owns the channel."""
    delivery_id = store.enqueue(channel="telegram", account=None, conversation="101", text="hi")
    token = store.issue_token("tg")
    async with _client() as client:
        connector = FakeConnector(client, token)
        assert (await connector.lease()).json() == {"deliveries": [], "paused": True}
        event = await connector.send_event("tg:1", "u1", "101", "hi", cursor=1)
        assert (event.status_code, event.json()["reason"]) == (409, "runtime_not_connector")
        cursor = await client.post("/connector/v1/cursor", json={"cursor": 2}, headers=connector.headers)
        assert cursor.json()["reason"] == "runtime_not_connector"
        state = (await client.get("/connector/v1/state", headers=connector.headers)).json()
        assert state["runtime_active"] is False
        set_channel_runtime(colony / "woltspace.json", "telegram", "connector")
        leased = (await connector.lease()).json()
        assert [d["delivery_id"] for d in leased["deliveries"]] == [delivery_id]
        assert (await connector.send_event("tg:1", "u1", "101", "hi", cursor=1)).json()["accepted"] is True
    # The refused event was not recorded: it was answered once the connector owned the channel.
    assert store._queue("tg").inbound


async def test_an_in_flight_poll_from_the_losing_side_cannot_lease_or_advance_after_the_switch(colony, store):
    """Switching back to the adapter mid-poll: the connector's long poll ends empty and paused."""
    config = colony / "woltspace.json"
    set_channel_runtime(config, "telegram", "connector")
    token = store.issue_token("tg")
    async with _client() as client:
        connector = FakeConnector(client, token)
        assert (await connector.send_event("tg:1", "u1", "101", "hi", cursor=1)).json()["accepted"] is True

        async def switch_back():
            await asyncio.sleep(0.05)
            set_channel_runtime(config, "telegram", "adapter")
            store.enqueue(channel="telegram", account=None, conversation="101", text="for the adapter")

        response, _ = await asyncio.gather(connector.lease(1, wait=5), switch_back())
        assert response.json() == {"deliveries": [], "paused": True}
        cursor = await client.post("/connector/v1/cursor", json={"cursor": 2}, headers=connector.headers)
        assert (cursor.status_code, cursor.json()["reason"]) == (409, "runtime_not_connector")
        assert (await client.get("/connector/v1/state", headers=connector.headers)).json()["cursor"] is None
        # Switch forward again: the queued message is still there, the cursor advances.
        set_channel_runtime(config, "telegram", "connector")
        leased = (await connector.lease(1)).json()["deliveries"]
        assert [d["text"] for d in leased] == ["for the adapter"]
        assert (await client.post("/connector/v1/cursor", json={"cursor": 2}, headers=connector.headers)).json()["cursor"] == 2


@pytest.mark.parametrize("runtime", ["adapter", "connector"])
async def test_notify_uses_exactly_one_outbound_route(colony, store, monkeypatch, runtime):
    set_channel_runtime(colony / "woltspace.json", "telegram", runtime)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:fake")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "101")
    sent = AsyncMock(return_value=None)
    monkeypatch.setitem(
        notify.SENDERS, "telegram",
        Plugin("telegram", sent, secrets=("TELEGRAM_BOT_TOKEN",), max_attachment_bytes=1000),
    )
    monkeypatch.setattr(notify, "append_chat_history", lambda *a: None)
    result = await notify._send("telegram", "softie-1", "hello", "101")
    queued = [d for d in store.list_deliveries("tg") if d["status"] == "queued"]
    if runtime == "connector":
        sent.assert_not_awaited()
        assert result["via"] == "connector" and result["delivery_id"] == queued[0]["delivery_id"]
        assert (queued[0]["text"], queued[0]["session"]) == ("hello", "softie-1")
        assert "reply to this message" in queued[0]["reply_footer"]
    else:
        sent.assert_awaited_once()
        assert queued == [] and "via" not in result


async def test_notify_under_the_connector_needs_no_lodge_token_but_a_bound_chat(colony, store, monkeypatch):
    set_channel_runtime(colony / "woltspace.json", "telegram", "connector")
    monkeypatch.setattr(notify, "dotenv_env", lambda key: "")
    monkeypatch.setattr(notify, "append_chat_history", lambda *a: None)
    assert (await notify._send("telegram", "", "hello", "101"))["via"] == "connector"
    with pytest.raises(notify.NoNotificationTarget, match="bind the chat"):
        await notify._send("telegram", "", "hello", "999")


def test_the_planner_does_not_start_the_adapter_under_the_connector_runtime(colony, monkeypatch):
    layout = RuntimeLayout.from_env()
    config = layout.platform_state / "config.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"channels": {"telegram": {"enabled": True, "token": "123:fake"}}}))
    env = {"WOLTSPACE_ENTRYPOINT": "1", "PATH": "/usr/bin"}
    assert TelegramConnector().plan(layout, env).enabled is True
    set_channel_runtime(colony / "woltspace.json", "telegram", "connector")
    plan = TelegramConnector().plan(layout, env)
    assert plan.enabled is False
    assert "does not poll" in plan.detail
    assert "old adapter" in plan.remedy and "stop the lodge" in plan.remedy
    set_channel_runtime(colony / "woltspace.json", "telegram", "adapter")
    assert TelegramConnector().plan(layout, env).enabled is True


def test_cli_runtime_and_cursor_verbs(colony, capsys):
    assert cli_main(["connector", "runtime", "telegram", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["runtime"] == "adapter"
    assert cli_main(["connector", "runtime", "telegram", "connector"]) == 0
    out = capsys.readouterr().out
    assert "telegram.runtime = connector" in out and "next `woltspace start`" in out
    assert channel_runtime(colony / "woltspace.json", "telegram") == "connector"
    assert cli_main(["connector", "add", "tg", "--channel", "telegram", "--account", "bot:1"]) == 0
    capsys.readouterr()
    assert cli_main(["connector", "cursor", "tg", "41", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["cursor"] == 41
    store = ConnectorStore(colony / ".space" / "connectors")
    queue = store._queue("tg")
    assert (queue.cursor, queue.answered_cursor) == (41, 40)
    assert cli_main(["connector", "cursor", "tg", "--clear"]) == 0
    assert store._queue("tg").cursor is None
    assert cli_main(["connector", "runtime", "telegram", "adapter"]) == 0
    assert channel_runtime(colony / "woltspace.json", "telegram") == "adapter"


def test_a_live_switch_is_refused_under_the_lodges_own_lock_and_a_start_cannot_race_it(colony, capsys, monkeypatch):
    """Finding 1: the switch takes the control-plane lock `woltspace start` holds for life."""
    from woltspace.instance import DataRootLock, InstanceConflict
    from server import connectors as connectors_module

    layout = RuntimeLayout.from_env()
    running = DataRootLock(layout, "the-lodge")
    running.acquire()
    try:
        assert cli_main(["connector", "runtime", "telegram", "connector"]) == 1
        err = capsys.readouterr().err
        assert "lodge is running" in err and "stop it" in err
        assert channel_runtime(colony / "woltspace.json", "telegram") == "adapter"
        # Reading is always fine.
        assert cli_main(["connector", "runtime", "telegram"]) == 0
    finally:
        running.release()
    # While the switch writes, a start that races it is refused by the same lock.
    raced = []
    real = connectors_module.set_channel_runtime

    def write_while_starting(path, channel, runtime):
        try:
            DataRootLock(layout, "racing-start").acquire()
        except InstanceConflict:
            raced.append("refused")
        return real(path, channel, runtime)

    monkeypatch.setattr(connectors_module, "set_channel_runtime", write_while_starting)
    assert cli_main(["connector", "runtime", "telegram", "connector"]) == 0
    assert raced == ["refused"]
    assert channel_runtime(colony / "woltspace.json", "telegram") == "connector"
    # The lock is given back: a start can take it now.
    DataRootLock(layout, "later-start").acquire()


def test_a_running_connector_makes_the_switch_refuse_and_the_switch_holds_both_locks(colony, store, capsys, monkeypatch):
    """Finding 1: the standalone connector's run lock counts; the write happens under both locks."""
    import fcntl
    from woltspace.instance import DataRootLock
    from server import connectors as connectors_module

    layout = RuntimeLayout.from_env()
    run_lock = connectors_module.run_lock_path(store.root, "telegram")
    # A connector process holds the channel's run lock for its whole life.
    holder = open(run_lock, "a+")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert cli_main(["connector", "runtime", "telegram", "connector"]) == 1
        err = capsys.readouterr().err
        assert "connector process is running" in err
        assert channel_runtime(colony / "woltspace.json", "telegram") == "adapter"
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()

    def held(path):
        with open(path, "a+") as probe:
            try:
                fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
            return False

    observed = []
    real = connectors_module.set_channel_runtime

    def observing(path, channel, runtime):
        observed.append((held(run_lock), held(layout.platform_state / "control-plane.lock")))
        return real(path, channel, runtime)

    monkeypatch.setattr(connectors_module, "set_channel_runtime", observing)
    assert cli_main(["connector", "runtime", "telegram", "connector"]) == 0
    assert observed == [(True, True)]
    assert not held(run_lock) and not held(layout.platform_state / "control-plane.lock")
    assert channel_runtime(colony / "woltspace.json", "telegram") == "connector"
    DataRootLock(layout, "later-start").acquire()
