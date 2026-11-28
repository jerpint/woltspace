"""The WhatsApp card launches the right skill, and setup keeps secrets local."""
import hashlib
import json
import stat
from pathlib import Path
from unittest.mock import Mock

import pytest
from starlette.testclient import TestClient

from server import app as server_app
from woltspace import whatsapp_setup
from woltspace.layout import RuntimeLayout

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("harness,delivery,expected", [
    ("claude", "copy", "/woltspace-setup-whatsapp"),
    ("codex", "copy", "@woltspace-setup-whatsapp"),
    ("claude", "plugin", "/woltspace:setup-whatsapp"),
    ("codex", "plugin", "@woltspace:setup-whatsapp"),
])
def test_setup_action_launches_the_delivered_skill(monkeypatch, tmp_path, harness, delivery, expected):
    root = tmp_path / "w"
    wolt = root / "setup-wolt" / "wolt"
    wolt.mkdir(parents=True)
    (wolt / "wolt.json").write_text(json.dumps({
        "name": "Setup Wolt", "type": "otter", "skills_delivery": delivery,
    }))
    monkeypatch.setattr(server_app, "WOLTS_DIR", root)
    monkeypatch.setattr(server_app, "wolt_harness", lambda _: harness)
    start = Mock(return_value={"name": "setup-session"})
    monkeypatch.setattr(server_app, "start_session", start)
    client = TestClient(server_app.app, base_url="http://localhost:7777")
    result = client.post("/sessions/new/lodge", json={"wolt": "setup-wolt", "setup": "whatsapp"})
    assert result.json() == {"name": "setup-session"}
    assert start.call_args.kwargs["wolt"] == "setup-wolt"
    assert start.call_args.kwargs["prompt"].split()[0] == expected
    assert start.call_args.kwargs["routing"] == {"adapter": "lodge"}


@pytest.fixture
def setup_terminal(tmp_path, monkeypatch):
    layout = RuntimeLayout(tmp_path / "w", ROOT, "127.0.0.1", 7798, "host")
    path = layout.platform_state / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"other": "keep", "channels": {"telegram": {"enabled": True}}}))
    monkeypatch.setattr(whatsapp_setup.sys, "stdin", Mock(isatty=lambda: True))
    for var in ("WOLTSPACE_CONFIG", "WHATSAPP_AGENT_TOKEN", "ENABLE_WHATSAPP_AGENT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(whatsapp_setup, "inspect_instance", Mock(return_value={
        "state": "healthy", "health": {"connectors": [{"name": "whatsapp", "state": "disabled"}]},
    }))
    monkeypatch.setattr(whatsapp_setup.getpass, "getpass", lambda _: "synthetic-key")
    return layout, path


def test_hidden_key_and_discovered_creator_are_saved_privately(setup_terminal, monkeypatch, capsys):
    layout, path = setup_terminal
    answers = Mock(side_effect=["", ""])
    monkeypatch.setattr("builtins.input", answers)
    discover = Mock(return_value="user:creator")
    monkeypatch.setattr(whatsapp_setup, "discover_creator", discover)
    assert "woltspace restart" in whatsapp_setup.configure(layout)
    key, marker, timestamp = discover.call_args.args
    assert key == "synthetic-key" and marker.startswith("wa-setup-") and timestamp > 0
    output = capsys.readouterr().out
    assert marker in output
    assert key not in output
    assert json.loads(path.read_text()) == {"other": "keep", "channels": {
        "telegram": {"enabled": True},
        "whatsapp": {"enabled": True, "agent_token": "synthetic-key", "owners": ["user:creator"]},
    }}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "key, creator, rejected",
    [
        ("synthetic-key", "user:original", False),
        ("synthetic-key", "user:different", True),
        ("fresh-key", "user:different", False),
    ],
)
def test_setup_checks_the_persisted_creator(setup_terminal, monkeypatch, key, creator, rejected):
    layout, path = setup_terminal
    original_config = path.read_bytes()
    state = layout.state_root / "whatsapp"
    state.mkdir()
    account = hashlib.sha256(b"synthetic-key").hexdigest()[:20]
    binding = state / f"owner-{account}.json"
    binding.write_text(json.dumps({"account": account, "participant": "user:original"}))
    binding.chmod(0o600)
    original_binding = binding.read_bytes()
    cursor = state / f"cursor-{account}.json"
    cursor.write_text('{"offset": 7}')
    monkeypatch.setattr(whatsapp_setup.getpass, "getpass", lambda _: key)
    monkeypatch.setattr("builtins.input", lambda _: creator)

    if rejected:
        with pytest.raises(whatsapp_setup.SetupError, match="use the bound creator or a fresh key"):
            whatsapp_setup.configure(layout)
        assert path.read_bytes() == original_config
    else:
        whatsapp_setup.configure(layout)
        assert json.loads(path.read_text())["channels"]["whatsapp"]["owners"] == [creator]
    assert binding.read_bytes() == original_binding
    assert cursor.read_text() == '{"offset": 7}'


def test_running_connector_does_not_prompt_or_write(setup_terminal, monkeypatch):
    layout, path = setup_terminal
    original = path.read_bytes()
    whatsapp_setup.inspect_instance.return_value["health"]["connectors"][0]["state"] = "running"
    prompt = Mock(side_effect=AssertionError("running connector should not prompt"))
    monkeypatch.setattr(whatsapp_setup.getpass, "getpass", prompt)
    assert "already running" in whatsapp_setup.configure(layout)
    assert path.read_bytes() == original
    prompt.assert_not_called()


def test_discovery_matches_only_fresh_marker(monkeypatch):
    def response(status, updates):
        return Mock(status=status, **{"read.return_value": json.dumps(updates).encode(),
                                      "__enter__": lambda self: self, "__exit__": lambda *args: None})

    def update(messages, offset):
        return {"next_offset": offset, "entry": [{"changes": [{"value": {"messages": messages}}]}]}

    old = {"type": "text", "from": "user:old", "timestamp": "99", "text": {"body": "marker"}}
    wrong = {**old, "from": "user:other", "timestamp": "101", "text": {"body": "unrelated"}}
    correct = {**old, "from": "user:creator", "timestamp": "101"}
    opener = Mock()
    opener.open.side_effect = [response(200, update([old, wrong], 3)), response(200, update([correct], 4))]
    monkeypatch.setattr(whatsapp_setup, "build_opener", lambda *_: opener)
    assert whatsapp_setup.discover_creator("synthetic-key", "marker", 100) == "user:creator"
    request = opener.open.call_args.args[0]
    assert "offset=3" in request.full_url
    assert request.get_header("Authorization") == "Bearer synthetic-key"
