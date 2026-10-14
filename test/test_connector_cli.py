"""`woltspace connector` is the owner's side of the connector protocol: instances,
bindings and credentials, written through the lodge's own store."""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container" / "lib"))

from woltspace.cli import main as cli_main  # noqa: E402
from server.connectors import ConnectorStore  # noqa: E402


@pytest.fixture
def colony(tmp_path, monkeypatch):
    wolts = tmp_path / "wolts"
    wolts.mkdir()
    for name in ("WOLTSPACE_WOLTS_DIR", "WOLTS_DIR"):
        monkeypatch.setenv(name, str(wolts))
    monkeypatch.setenv("WOLTSPACE_DIR", str(ROOT))
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    return wolts


def _out(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def test_add_bind_enable_token_disable_unbind_remove(colony, capsys):
    assert cli_main(["connector", "add", "tg-main", "--channel", "telegram", "--account", "bot:1", "--json"]) == 0
    added = _out(capsys)["instance"]
    assert (added["instance_id"], added["enabled"], added["generation"]) == ("tg-main", False, 0)

    assert cli_main([
        "connector", "bind", "tg-main", "--sender", "u1", "--conversation", "c1",
        "--wolt", "softie", "--may-wake", "--json",
    ]) == 0
    binding = _out(capsys)["binding"]
    assert binding.pop("binding_id")
    assert binding == {
        "instance_id": "tg-main", "account": "bot:1", "sender": "u1", "conversation": "c1",
        "thread": None, "default_wolt": "softie", "may_wake": True,
    }

    assert cli_main(["connector", "enable", "tg-main"]) == 0
    capsys.readouterr()
    assert cli_main(["connector", "token", "tg-main", "--json"]) == 0
    captured = capsys.readouterr()
    token = json.loads(captured.out)["token"]
    assert token.startswith("wct_") and "revoked" in captured.err
    store = ConnectorStore(colony / ".space" / "connectors")
    who = store.authenticate(token)
    assert (who.instance_id, who.generation, who.enabled) == ("tg-main", 1, True)
    registry = json.loads((colony / ".space" / "connectors" / "registry.json").read_text())
    assert token not in json.dumps(registry)

    assert cli_main(["connector", "list"]) == 0
    listing = capsys.readouterr().out
    assert "tg-main  telegram  account=bot:1  enabled  generation=1" in listing
    assert "sender=u1 conversation=c1 -> softie may-wake" in listing

    assert cli_main(["connector", "token", "tg-main", "--revoke", "--json"]) == 0
    assert _out(capsys)["revoked"] == 1
    assert store.authenticate(token) is None

    assert cli_main(["connector", "disable", "tg-main"]) == 0
    assert cli_main(["connector", "unbind", "tg-main", "--sender", "u1", "--conversation", "c1", "--json"]) == 0
    capsys.readouterr()
    assert cli_main(["connector", "remove", "tg-main", "--json"]) == 0
    assert _out(capsys) == {"ok": True, "cancelled": 0, "transferred": 0}
    assert cli_main(["connector", "list", "--json"]) == 0
    assert _out(capsys) == {"instances": [], "bindings": [], "ledger": {"readable": True, "debts": [], "error": None}}


def test_one_enabled_instance_per_channel_and_refusals_exit_1(colony, capsys):
    assert cli_main(["connector", "add", "a", "--channel", "telegram", "--account", "bot:1"]) == 0
    assert cli_main(["connector", "add", "b", "--channel", "telegram", "--account", "bot:2"]) == 0
    assert cli_main(["connector", "add", "a", "--channel", "telegram", "--account", "bot:1"]) == 1
    assert "instance_exists" in capsys.readouterr().err
    assert cli_main(["connector", "enable", "a"]) == 0
    assert cli_main(["connector", "enable", "b"]) == 1
    assert "already serves telegram" in capsys.readouterr().err
    assert cli_main(["connector", "add", "Bad Id", "--channel", "telegram", "--account", "x"]) == 1
    assert cli_main(["connector", "bind", "a", "--sender", "u1", "--conversation", "c1", "--wolt", "Not/A/Wolt"]) == 1
    assert cli_main(["connector", "token", "nobody"]) == 1
    assert "unknown_instance" in capsys.readouterr().err


def test_remove_is_cancel_only_and_recovery_runs_before_every_verb(colony, capsys):
    """Transfer is not in this slice: removal cancels, and an interrupted removal
    is finished by the next command."""
    assert cli_main(["connector", "add", "old", "--channel", "telegram", "--account", "bot:1"]) == 0
    assert cli_main(["connector", "bind", "old", "--sender", "u1", "--conversation", "c1", "--wolt", "softie"]) == 0
    assert cli_main(["connector", "enable", "old"]) == 0
    store = ConnectorStore(colony / ".space" / "connectors")
    for text in ("one", "two"):
        store.enqueue(channel="telegram", account="bot:1", conversation="c1", text=text)
    capsys.readouterr()
    assert cli_main(["connector", "queue", "old"]) == 0
    assert capsys.readouterr().out.count("queued") == 2
    with pytest.raises(SystemExit):
        cli_main(["connector", "remove", "old", "--transfer-to", "new"])
    capsys.readouterr()
    assert cli_main(["connector", "remove", "old", "--json"]) == 0
    assert _out(capsys) == {"ok": True, "cancelled": 2, "transferred": 0}
    assert len((colony / ".space" / "connectors" / "failures.jsonl").read_text().splitlines()) == 2

    # An orphan queue (registry written, queue not yet cancelled) is recovered
    # by whatever command runs next.
    assert cli_main(["connector", "add", "late", "--channel", "telegram", "--account", "bot:3"]) == 0
    assert cli_main(["connector", "bind", "late", "--sender", "u1", "--conversation", "c1", "--wolt", "softie"]) == 0
    assert cli_main(["connector", "enable", "late"]) == 0
    store.enqueue(channel="telegram", account="bot:3", conversation="c1", text="stranded")
    registry_path = colony / ".space" / "connectors" / "registry.json"
    registry = json.loads(registry_path.read_text())
    del registry["instances"]["late"]
    registry_path.write_text(json.dumps(registry))
    capsys.readouterr()
    assert cli_main(["connector", "list", "--json"]) == 0
    assert _out(capsys)["instances"] == []
    assert not (colony / ".space" / "connectors" / "queues" / "late.json").exists()
    assert len((colony / ".space" / "connectors" / "failures.jsonl").read_text().splitlines()) == 3


def test_unreadable_state_is_a_message_and_nothing_is_written(colony, capsys):
    registry = colony / ".space" / "connectors" / "registry.json"
    registry.parent.mkdir(parents=True)
    registry.write_text("{broken")
    assert cli_main(["connector", "list"]) == 1
    assert "does not parse" in capsys.readouterr().err
    assert cli_main(["connector", "add", "a", "--channel", "telegram", "--account", "bot:1"]) == 1
    assert registry.read_text() == "{broken"


def test_bare_connector_prints_its_verbs(colony, capsys):
    assert cli_main(["connector"]) == 1
    assert "bind" in capsys.readouterr().out


def test_handoffs_are_listed_retried_and_cancelled_by_their_displayed_event_id(colony, capsys):
    """Astre's probe: the owner types what the listing shows, never an internal key."""
    assert cli_main(["connector", "add", "tg", "--channel", "fake", "--account", "acct"]) == 0
    assert cli_main(["connector", "bind", "tg", "--sender", "u", "--conversation", "c", "--wolt", "softie"]) == 0
    assert cli_main(["connector", "enable", "tg"]) == 0
    # The lodge accepts two events whose handoff fails; the CLI sees them parked.
    lodge = ConnectorStore(colony / ".space" / "connectors")
    lodge.router = lambda event: (_ for _ in ()).throw(RuntimeError("session unreachable"))
    who = lodge.authenticate(lodge.issue_token("tg"))
    for event_id in ("tg:41", "tg:42"):
        lodge.accept_event(who, {"event_id": event_id, "sender": {"provider_id": "u"}, "conversation": {"provider_id": "c"}, "text": "hi"})
    capsys.readouterr()
    assert cli_main(["connector", "handoffs", "tg"]) == 0
    out = capsys.readouterr().out
    assert "tg:41  accepted  attempts=1  session unreachable" in out and "tg:42" in out
    assert "\x00" not in out
    assert cli_main(["connector", "handoffs", "tg", "--retry", "tg:41", "--json"]) == 0
    assert _out(capsys)["retried"] is True
    record = lodge._queue("tg").inbound["acct\x00tg:41"]
    assert (record["attempts"], record["claim"]) == (0, None)
    assert cli_main(["connector", "handoffs", "tg", "--cancel", "tg:42", "--json"]) == 0
    assert _out(capsys)["cancelled"] is True
    assert cli_main(["connector", "handoffs", "tg", "--json"]) == 0
    assert [h["event_id"] for h in _out(capsys)["handoffs"]] == ["tg:41"]
    assert cli_main(["connector", "handoffs", "tg", "--retry", "tg:99"]) == 0
    assert "no such pending event" in capsys.readouterr().out
