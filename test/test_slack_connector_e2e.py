"""Slack over the real socket: the connector module and the lodge's ChatLodge, each on its
own side of a unix socket (connector_client, connector_broker), Slack and the lodge's
sessions faked at their edges, as test_connector_e2e.py does for Telegram."""

import asyncio
import importlib
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for part in ("container", "container/lib", "container/connectors/slack"):
    sys.path.insert(0, str(ROOT / part))

import connector_broker  # noqa: E402
import connector_client  # noqa: E402
from test_slack_connector import DM, LIVE, OWNER, FakeSlack, dm  # noqa: E402


def test_a_dm_resent_by_slack_is_delivered_once_and_a_click_edits_its_picker(monkeypatch):
    root = Path(tempfile.mkdtemp(prefix="wss", dir="/tmp"))
    wolts = root / "w"
    for name in ("alpha", "beta"):
        (wolts / name / "wolt").mkdir(parents=True)
        (wolts / name / "wolt" / "wolt.json").write_text('{"type": "beaver"}')
    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(wolts))
    monkeypatch.setenv("SLACK_OWNER_USER", OWNER)
    import chat_lodge
    import connector_registry
    sc = importlib.import_module("slack_connector")
    pasted = []
    for name, value in [("message_session", lambda n, t: pasted.append(n) or {"ok": True, "url": ""}),
                        ("get_tunnel_url", lambda: ""), ("dog_available", lambda: False),
                        ("_bot_log", lambda *a: None),
                        ("list_wolts", lambda: [{"name": "alpha", "type": "beaver"},
                                                {"name": "beta", "type": "beaver"}])]:
        monkeypatch.setattr(chat_lodge, name, value)
    side = chat_lodge.ChatLodge(connector_registry.spec("slack"), [OWNER])
    side._save_chat_state(DM, {"active_wolt": "alpha", "active_session": LIVE})
    api = FakeSlack()
    for name, value in [("API", api), ("PENDING", type(sc.PENDING)()), ("_SEEN", type(sc._SEEN)()),
                        ("COMMANDS", ("wolt",))]:
        monkeypatch.setattr(sc, name, value)
    run_dir, seen = wolts / ".space" / "run" / "connectors", set()

    def fresh(name, event_id):
        new = (name, event_id) not in seen
        seen.add((name, event_id))
        return new

    async def main():
        broker = connector_broker.Broker(run_dir, fresh)
        broker.add("slack", side.handle, [("wolt", "switch")])
        await broker.start()
        client = connector_client.Client(
            "slack", socket_path=str(connector_broker.socket_path(run_dir, "slack")),
            token=connector_broker.new_token(run_dir, "slack"),
            on_say=sc.on_say, on_edit=sc.on_edit, on_ack=sc.on_ack, on_send=sc.on_send)
        monkeypatch.setattr(sc, "CLIENT", client)
        await client.connect()

        async def ack():
            pass
        for _ in range(2):  # Slack resends an event it thinks was not received
            await sc.on_message(*dm("hello"))
        await sc.on_message(*dm("!wolt", event_id="Ev0002"))
        await broker.drain("slack")
        await client.ping()
        await sc.on_action(ack, {"user": {"id": OWNER}, "channel": {"id": DM}, "message": {"ts": "2.000"},
                                 "actions": [{"value": "wolt:beta", "action_ts": "3"}]})
        await broker.drain("slack")
        await client.ping()
        await client.close()
        await broker.stop()
    try:
        asyncio.run(main())
        assert pasted == [LIVE]
        assert [post[1].split("\n")[0] for post in api.posts] == [f"🪵 sent to {LIVE}", "active: 🦫 alpha"]
        assert api.updates[-1][:3] == (DM, "2.000", "active: 🦫 beta\n\ntap to switch")
        assert side._load_chat_state(DM)["active_wolt"] == "beta"
    finally:
        shutil.rmtree(root, ignore_errors=True)
