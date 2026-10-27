import importlib.util
import json
import os
import secrets
import sys
import stat
import tomllib
import urllib.request
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT), str(ROOT / "container" / "lib")]
SPEC = importlib.util.spec_from_file_location(
    "whatsapp_owner_test_script", ROOT / "scripts" / "whatsapp_owner_test.py"
)
owner_test = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(owner_test)


def test_private_credential_store_has_required_permissions(tmp_path, monkeypatch):
    private = tmp_path / "private-test"
    monkeypatch.setattr(owner_test, "PRIVATE", private)
    monkeypatch.setattr(owner_test, "TOKEN_FILE", private / "agent-key")
    owner_test._store_token("synthetic-test-placeholder")
    assert stat.S_IMODE(private.stat().st_mode) == 0o700
    assert stat.S_IMODE(owner_test.TOKEN_FILE.stat().st_mode) == 0o600
    assert owner_test._read_token() == "synthetic-test-placeholder"


def test_owner_child_environment_drops_whatsapp_secret(monkeypatch):
    monkeypatch.setenv("WHATSAPP_AGENT_TOKEN", "synthetic-test-placeholder")
    child_env = owner_test._child_environment()
    assert "WHATSAPP_AGENT_TOKEN" not in child_env
    assert os.environ["WHATSAPP_AGENT_TOKEN"] == "synthetic-test-placeholder"


@pytest.mark.parametrize("mode", ["--transport-run", "--notify-run"])
@pytest.mark.parametrize("ambiguous", [False, True])
def test_full_isolated_runner_modes_use_mocked_network_and_private_environment(
    tmp_path, monkeypatch, capsys, mode, ambiguous
):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    monkeypatch.setattr(owner_test, "PRIVATE", private)
    monkeypatch.setattr(owner_test, "_read_token", lambda: "synthetic-test-token")
    monkeypatch.setattr(secrets, "token_hex", lambda _size: "abcd1234")
    monkeypatch.setattr(sys, "argv", [str(ROOT / "scripts" / "whatsapp_owner_test.py"), mode])

    phrase = "wa-check-abcd1234"
    timestamp = int(time.time()) + 5
    def message(mid, body):
        return {"id": mid, "from": "user:99", "timestamp": str(timestamp),
                "type": "text", "text": {"body": body}}
    updates = ([message("select", "wolt wa-test"), message("challenge", phrase),
                message("duplicate", phrase)] if mode == "--transport-run"
               else [message("challenge", phrase), message("duplicate", phrase)])
    get_count = 0
    sent_posts = []
    ambiguous_attempts = []

    class Response:
        status = 200
        def __init__(self, body):
            self.body = body
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return json.dumps(self.body).encode()

    def fake_urlopen(request, timeout=0):
        nonlocal get_count
        if request.get_method() == "GET":
            batch = updates if get_count == 0 else []
            get_count += 1
            return Response({"next_offset": get_count, "entry": [{"changes": [{"value": {"messages": batch}}]}]})
        payload = json.loads(request.data.decode())
        sent_posts.append(payload)
        if ambiguous and "transport check succeeded" in payload["text"]["body"]:
            ambiguous_attempts.append(payload)
            raise TimeoutError("synthetic ambiguous send")
        return Response({"messages": [{"id": "sent"}]})

    class FakeAsyncClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def post(self, url, *, headers, json):
            assert headers["Authorization"] == "Bearer synthetic-test-token"
            assert "api.whatsapp.com" in url
            sent_posts.append(json)
            if ambiguous:
                ambiguous_attempts.append(json)
                raise TimeoutError("synthetic ambiguous notify send")
            return type("HttpResponse", (), {"status_code": 200, "headers": {},
                                              "json": lambda self: {"messages": [{"id": "sent"}]}})()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    # Force fresh imports so the test exercises the runner's complete setup.
    names = ("bot.whatsapp_adapter", "server.notify", "server.config",
             "server.notification_senders", "server.notification_types",
             "server.speech", "server.state", "sessions", "bot.core", "wolts",
             "lib", "lib.paths", "lib.notify_prompt")
    saved = {name: sys.modules.pop(name) for name in names if name in sys.modules}
    missing = object()
    parent_attrs = {}
    for name in names:
        parent_name, _, attribute = name.rpartition(".")
        parent = sys.modules.get(parent_name)
        if parent is not None:
            parent_attrs[(parent, attribute)] = getattr(parent, attribute, missing)
    previous_env = os.environ.copy()
    previous_path = list(sys.path)
    try:
        result = owner_test._isolated_run()
    finally:
        os.environ.clear()
        os.environ.update(previous_env)
        sys.path[:] = previous_path
        for name in names:
            sys.modules.pop(name, None)
        sys.modules.update(saved)
        for (parent, attribute), value in parent_attrs.items():
            if value is missing:
                parent.__dict__.pop(attribute, None)
            else:
                setattr(parent, attribute, value)
    assert result == (2 if ambiguous else 0)
    output = capsys.readouterr().out
    if ambiguous:
        assert "ended ambiguously" in output
        assert len(ambiguous_attempts) == 1
    else:
        assert "One owner message received and one canned WhatsApp reply sent." in output
    assert get_count >= 1
    if mode == "--transport-run":
        assert len([p for p in sent_posts if "transport check succeeded" in p["text"]["body"]]) <= 1
        if not ambiguous:
            # Selection confirmation plus exactly one test reply.
            assert len(sent_posts) == 2
    else:
        assert len([p for p in sent_posts if "notify path check succeeded" in p["text"]["body"]]) == 1
    assert all(payload["to"] == "user:99" for payload in sent_posts)
    assert not list(private.iterdir())


def test_owner_setup_fails_closed_when_no_real_terminal(monkeypatch):
    monkeypatch.setattr(owner_test, "_require_tty", lambda: (_ for _ in ()).throw(
        RuntimeError("no terminal")
    ))
    monkeypatch.setattr(owner_test.getpass, "getpass", lambda *args, **kwargs: pytest.fail("credential prompt must not run"))
    with pytest.raises(RuntimeError, match="no terminal"):
        owner_test._setup()


def test_packaged_skill_tree_contains_whatsapp_start_chat_mode():
    skill = ROOT / "container" / "skills" / "start-chat"
    mode = skill / "modes" / "whatsapp.md"
    package = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert mode.is_file()
    assert "whatsapp" in (skill / "SKILL.md").read_text().lower()
    assert package["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]["container"]
    assert "/container" in package["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
