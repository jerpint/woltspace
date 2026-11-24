"""API contract for native targets, capabilities, and Auto consent."""

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

import harnesses
from server import app as app_module


async def _request(method, path, **kwargs):
    transport = httpx.ASGITransport(app=app_module.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://localhost:7777",
    ) as client:
        return await client.request(method, path, **kwargs)


def _layout(tmp_path, monkeypatch):
    import paths
    import sessions

    wolts = tmp_path / "wolts"
    home = wolts / "maple"
    (home / "wolt").mkdir(parents=True)
    (home / "wolt" / "wolt.json").write_text(
        json.dumps({"name": "maple", "type": "raccoon"})
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(app_module, "WOLTS_DIR", wolts)
    monkeypatch.setattr(sessions, "WOLTS_DIR", wolts)
    monkeypatch.setattr(paths, "WOLTS_DIR", wolts)
    return wolts, home, repo


def test_runtime_capabilities_distinguish_host_from_external(monkeypatch):
    monkeypatch.setenv("WOLTSPACE_ISOLATION", "host")
    response = asyncio.run(_request("GET", "/runtime/capabilities"))
    assert response.status_code == 200
    assert response.json() == {
        "isolation": "host",
        "supports_host_workdirs": True,
        "default_execution_policy": "prompt",
        "policy_version": 1,
        "record_version": 2,
        "levels": harnesses.levels("host"),
        "default_harness": harnesses.get_default_harness(),
    }
    assert response.json()["levels"]["pi"] == {"bypass": "Bypass"}


def test_health_identifies_the_exact_control_plane(monkeypatch):
    monkeypatch.setenv("WOLTSPACE_INSTANCE_ID", "instance-abc")
    monkeypatch.setenv("WOLTSPACE_ISOLATION", "host")
    response = asyncio.run(_request("GET", "/health"))
    assert response.status_code == 200
    assert response.json()["instance_id"] == "instance-abc"
    assert response.json()["isolation"] == "host"


def test_wolt_list_exposes_absolute_home(tmp_path, monkeypatch):
    _, home, _ = _layout(tmp_path, monkeypatch)
    response = asyncio.run(_request("GET", "/wolts"))
    assert response.json()[0]["home"] == str(home.resolve())


def test_auto_grant_requires_exact_canonical_confirmation(tmp_path, monkeypatch):
    _, _, repo = _layout(tmp_path, monkeypatch)
    rejected = asyncio.run(_request("POST", "/auto-grants/grant", json={
        "wolt_id": "maple", "workdir": str(repo), "confirm": "yes",
    }))
    assert rejected.status_code == 400
    assert rejected.json()["canonical_workdir"] == str(repo.resolve())

    granted = asyncio.run(_request("POST", "/auto-grants/grant", json={
        "wolt_id": "maple", "workdir": str(repo),
        "confirm": str(repo.resolve()),
    }))
    assert granted.status_code == 200

    checked = asyncio.run(_request("POST", "/auto-grants/check", json={
        "wolt_id": "maple", "workdir": str(repo),
    }))
    assert checked.json()["approved"] is True

    revoked = asyncio.run(_request("POST", "/auto-grants/revoke", json={
        "wolt_id": "maple", "workdir": str(repo),
    }))
    assert revoked.json()["revoked"] is True


def test_lodge_spawn_passes_explicit_target_and_policy(tmp_path, monkeypatch):
    _, _, repo = _layout(tmp_path, monkeypatch)
    seen = {}

    def fake_start(**kwargs):
        seen.update(kwargs)
        return {"name": "maple-session", "wolt": "maple"}

    monkeypatch.setattr(app_module, "start_session", fake_start)
    response = asyncio.run(_request("POST", "/sessions/new/lodge", json={
        "wolt": "maple",
        "workdir": str(repo),
        "execution_policy": "prompt",
    }))
    assert response.status_code == 200
    assert seen["workdir"] == str(repo)
    assert seen["execution_policy"] == "prompt"


def test_lodge_reports_unapproved_auto_as_forbidden(monkeypatch):
    def denied(**kwargs):
        raise PermissionError("exact grant required")

    monkeypatch.setattr(app_module, "start_session", denied)
    response = asyncio.run(_request("POST", "/sessions/new/lodge", json={
        "wolt": "maple", "execution_policy": "auto",
    }))
    assert response.status_code == 403
    assert "exact grant" in response.json()["error"]


def test_create_wolt_passes_confirmed_target_and_policy(tmp_path, monkeypatch):
    import wolts

    _, _, repo = _layout(tmp_path, monkeypatch)
    seen = {}
    monkeypatch.setattr(
        wolts, "create_creature_wolt",
        lambda name, kind, **kwargs: seen.update({
            "scaffold_harness": kwargs.get("harness"),
        }),
    )

    def fake_start(**kwargs):
        seen.update(kwargs)
        return {"name": "newmaple-session", "wolt": "newmaple"}

    monkeypatch.setattr(app_module, "start_session", fake_start)
    response = asyncio.run(_request("POST", "/sessions/new/create", json={
        "name": "newmaple",
        "type": "raccoon",
        "workdir": str(repo),
        "execution_policy": "prompt",
    }))
    assert response.status_code == 200
    assert seen["wolt"] == "newmaple"
    assert seen["workdir"] == str(repo)
    assert seen["execution_policy"] == "prompt"
    assert seen["scaffold_harness"] == ""
    assert seen["harness"] == "claude"


@pytest.mark.parametrize("typed, slug, shown", [
    ("Wolter White", "wolter-white", "Wolter White"),
    ("Señor  Otter!", "senor-otter", "Señor Otter!"),
    ("chip", "chip", "chip"),
])
def test_create_wolt_slugifies_the_typed_name(tmp_path, monkeypatch, typed, slug, shown):
    import wolts

    _layout(tmp_path, monkeypatch)
    seen = {}
    monkeypatch.setattr(
        wolts, "create_creature_wolt",
        lambda name, kind, **kwargs: seen.update({"name": name, **kwargs}),
    )

    def fake_start(**kwargs):
        seen["session_wolt"] = kwargs["wolt"]
        return {"name": f"{slug}-session", "wolt": slug}

    monkeypatch.setattr(app_module, "start_session", fake_start)
    response = asyncio.run(_request("POST", "/sessions/new/create", json={
        "name": typed, "type": "raccoon",
    }))
    assert response.status_code == 200
    assert seen["name"] == slug
    assert seen["session_wolt"] == slug
    assert seen["display_name"] == shown


@pytest.mark.parametrize("typed", ["", "   ", "!!!", "123", "x" * 41])
def test_create_wolt_refuses_names_without_a_usable_slug(tmp_path, monkeypatch, typed):
    import wolts

    _layout(tmp_path, monkeypatch)
    created = []
    monkeypatch.setattr(
        wolts, "create_creature_wolt",
        lambda *args, **kwargs: created.append(args),
    )
    response = asyncio.run(_request("POST", "/sessions/new/create", json={
        "name": typed, "type": "raccoon",
    }))
    assert response.status_code == 400
    assert created == []


def test_create_wolt_refuses_a_taken_name_in_plain_words(tmp_path, monkeypatch):
    import wolts

    _layout(tmp_path, monkeypatch)
    created = []
    monkeypatch.setattr(
        wolts, "create_creature_wolt",
        lambda *args, **kwargs: created.append(args),
    )
    response = asyncio.run(_request("POST", "/sessions/new/create", json={
        "name": "Maple", "type": "raccoon",
    }))
    assert response.status_code == 409
    assert response.json() == {"detail": "a wolt named maple already exists"}
    assert created == []


def test_create_wolt_pins_a_chosen_model_with_its_harness(tmp_path, monkeypatch):
    import wolts

    _layout(tmp_path, monkeypatch)
    seen = {}
    monkeypatch.setattr(
        wolts, "create_creature_wolt",
        lambda name, kind, **kwargs: seen.update(kwargs),
    )
    monkeypatch.setattr(
        app_module, "start_session",
        lambda **kwargs: {"name": "newmaple-session", "wolt": "newmaple"},
    )
    response = asyncio.run(_request("POST", "/sessions/new/create", json={
        "name": "newmaple", "type": "raccoon", "model": "fable",
    }))
    assert response.status_code == 200
    assert seen["model"] == "fable"
    # the pin is only meaningful on the engine it was chosen for
    assert seen["harness"] == "claude"


@pytest.mark.parametrize("model", ["gpt-5.5", ["fable"]])
def test_create_wolt_refuses_a_model_the_harness_does_not_offer(
    tmp_path, monkeypatch, model,
):
    import wolts

    _layout(tmp_path, monkeypatch)
    created = []
    monkeypatch.setattr(
        wolts, "create_creature_wolt",
        lambda *args, **kwargs: created.append(args),
    )
    response = asyncio.run(_request("POST", "/sessions/new/create", json={
        "name": "newmaple", "type": "raccoon", "harness": "claude", "model": model,
    }))
    assert response.status_code == 400
    assert created == []


def test_harness_tiers_route_saves_and_validates(tmp_path, monkeypatch):
    _layout(tmp_path, monkeypatch)
    # the lodge settings file is found through the environment, not WOLTS_DIR
    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(tmp_path / "wolts"))
    ok = asyncio.run(_request("POST", "/harness/tiers", json={
        "harness": "claude", "tiers": {"otter": "fable"},
    }))
    assert ok.status_code == 200
    assert ok.json()["models"]["otter"] == "fable"
    saved = json.loads((tmp_path / "wolts" / "woltspace.json").read_text())
    assert saved["harness"]["models"]["claude"]["tiers"] == {"otter": "fable"}

    for body in (
        {"harness": "claude"},
        {"harness": "claude", "tiers": {"otter": "gpt-5.5"}},
        {"harness": "nope", "tiers": {"otter": "fable"}},
        {"harness": "claude", "tiers": {"otter": "fable"}, "extra": 1},
        ["claude"],
    ):
        bad = asyncio.run(_request("POST", "/harness/tiers", json=body))
        assert bad.status_code == 400, body


@pytest.mark.skipif(not shutil.which("node"), reason="TUI requires Node")
@pytest.mark.parametrize("harness", ["claude", "codex"])
@pytest.mark.parametrize("approved, override, expected", [
    (False, None, "prompt"),
    (True, None, "bypass"),
    (True, "prompt", "prompt"),
    (False, "auto", None),
    (True, "auto", "auto"),
])
def test_tui_payload_through_api_resolves_grants_and_harness_command(
    tmp_path, monkeypatch, fake_runtime, harness, approved, override, expected,
):
    """Run the TUI request builder, real API and session policy resolution.

    Only the process runtime is faked: no agent or live lodge is started.
    """
    import sessions
    from execution_policy import AutoGrantStore
    from session_targets import SessionTarget

    wolts, home, _ = _layout(tmp_path, monkeypatch)
    (home / "wolt" / "wolt.json").write_text(json.dumps({
        "name": "maple", "type": "raccoon", "harness": harness,
        "model": "sonnet" if harness == "claude" else "gpt-5.6-sol",
    }))
    monkeypatch.setenv("WOLTSPACE_ISOLATION", "host")
    if approved:
        AutoGrantStore(wolts).grant(SessionTarget.resolve("maple", home, wolts_dir=wolts))

    # Capture exactly what the JS API sends from a default TUI spawn target.
    script = """
        import { spawnTarget } from './src/session-view.js';
        import { spawnSession } from './src/api.js';
        globalThis.fetch = async (_url, options) => {
            console.log(options.body);
            return { ok: true, json: async () => ({}) };
        };
        const target = spawnTarget({
            supports_host_workdirs: true, default_execution_policy: 'prompt',
        }, { home: process.argv[1] }, '/unrelated-launch-dir');
        await spawnSession('maple', target.workdir,
            process.argv[2] || target.executionPolicy);
    """
    result = subprocess.run([
        "node", "--input-type=module", "-e", script, str(home), override or "",
    ], cwd=Path(__file__).resolve().parents[1] / "tui",
        capture_output=True, text=True, check=True)
    payload = json.loads(result.stdout)
    if override is None:
        assert "execution_policy" not in payload
    response = asyncio.run(_request("POST", "/sessions/new/lodge", json=payload))
    if expected is None:
        assert response.status_code == 403
        return
    assert response.status_code == 200, response.text
    session = response.json()
    assert session["execution_policy"]["mode"] == expected
    assert session["target"]["canonical_workdir"] == str(home.resolve())
    command = sessions.prepare_session_command(session["name"], "spawn")
    bypass_flag = ("--dangerously-skip-permissions" if harness == "claude"
                   else "--dangerously-bypass-approvals-and-sandbox")
    assert (bypass_flag in command) == (expected == "bypass")
    assert " ".join(harnesses.policy_flags(harness, session["execution_policy"])) in command


def test_a_level_the_engine_does_not_offer_is_a_400_with_the_reason(tmp_path, monkeypatch, fake_runtime):
    import sessions

    _, home, _ = _layout(tmp_path, monkeypatch)
    (home / "wolt" / "wolt.json").write_text(json.dumps({"name": "maple", "type": "raccoon", "harness": "hermes"}))
    monkeypatch.setenv("WOLTSPACE_ISOLATION", "external")
    response = asyncio.run(_request("POST", "/sessions/new/lodge",
                                    json={"wolt": "maple", "execution_policy": "auto"}))
    assert response.status_code == 400
    assert response.json()["error"].startswith("hermes does not offer Auto on external")
    assert sessions.SessionRegistry().list() == []  # refused before anything is recorded


def test_create_wolt_refuses_a_level_its_engine_does_not_offer_before_scaffolding(tmp_path, monkeypatch):
    import wolts

    _layout(tmp_path, monkeypatch)
    monkeypatch.setenv("WOLTSPACE_ISOLATION", "external")
    monkeypatch.setattr(app_module, "harness_install_problem", lambda *a, **k: "")
    created = []
    monkeypatch.setattr(wolts, "create_creature_wolt", lambda *args, **kwargs: created.append(args))
    response = asyncio.run(_request("POST", "/sessions/new/create", json={
        "name": "birch", "type": "raccoon", "harness": "codex", "execution_policy": "prompt",
    }))
    assert response.status_code == 400
    assert response.json()["detail"].startswith("codex does not offer Ask on external")
    assert created == []


@pytest.mark.parametrize("level,engine,isolation,want", [
    ("auto", "claude", "host", "auto"),
    ("bypass", "claude", "host", "prompt"),
    ("auto", "pi", "host", "prompt"),
    ("auto", "codex", "external", "bypass"),
    ({"private": "sentinel"}, "claude", "host", "prompt"),
])
def test_capability_default_respects_engine_and_consent(
    tmp_path, monkeypatch, caplog, level, engine, isolation, want
):
    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(tmp_path))
    monkeypatch.setenv("WOLTSPACE_ISOLATION", isolation)
    (tmp_path / "woltspace.json").write_text(json.dumps({
        "harness": {"default": engine},
        "sessions": {"default_execution_policy": level},
    }))
    caps = asyncio.run(_request("GET", "/runtime/capabilities")).json()
    assert caps["default_execution_policy"] == want
    assert not caplog.records  # capability reads never warn


@pytest.mark.parametrize("isolation,installed,detail", [
    ("external", True, "Antigravity requires a native Woltspace lodge."),
    ("host", False, "Antigravity is not installed (no `agy` on PATH)."),
])
def test_antigravity_rejects_unavailable_runtime(tmp_path, monkeypatch, fake_runtime,
                                                isolation, installed, detail):
    import sessions

    _, home, _ = _layout(tmp_path, monkeypatch)
    monkeypatch.setenv("WOLTSPACE_ISOLATION", isolation)
    monkeypatch.setattr(harnesses, "_engine_on_path", lambda binary: installed)
    (home / "wolt" / "wolt.json").write_text(json.dumps({
        "name": "maple", "type": "otter", "harness": "antigravity",
    }))
    response = asyncio.run(_request("POST", "/sessions/new/lodge", json={"wolt": "maple"}))
    assert response.status_code >= 400
    assert detail in response.text
    assert sessions.SessionRegistry().records() == []
    assert fake_runtime.spawns == []


def test_antigravity_launch_and_exact_resume(tmp_path, monkeypatch, fake_runtime, agent_comes_up):
    import os
    import shlex
    import sys
    from concurrent.futures import ThreadPoolExecutor
    from uuid import NAMESPACE_URL, uuid5

    import sessions
    from execution_policy import AutoGrantStore
    from session_targets import SessionTarget

    wolts, home, repo = _layout(tmp_path, monkeypatch)
    (home / "wolt" / "wolt.json").write_text(json.dumps({
        "name": "maple", "type": "otter", "harness": "antigravity",
    }))
    # An existing grant for another engine must not make the default Bypass.
    target = SessionTarget.resolve("maple", repo, wolts_dir=wolts)
    AutoGrantStore(wolts).grant(target)

    root = Path(__file__).resolve().parents[1]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_agy = bin_dir / "agy"
    fake_agy.write_text(f"#!{sys.executable}\n" + """\
import json
import os
import subprocess
import sys
from uuid import NAMESPACE_URL, uuid5

args = sys.argv[1:]
settings = json.loads(args[args.index("--settings") + 1])
if status_line := settings.get("statusLine"):
    # Two processes in the same cwd each report the ID created by that process.
    session = os.environ["WOLTSPACE_WOLT_SESSION"]
    conversation = str(uuid5(NAMESPACE_URL, session))
    reports = [
        ("initializing", str(uuid5(NAMESPACE_URL, session + "-setup"))),
        ("idle", conversation),
        ("idle", str(uuid5(NAMESPACE_URL, session + "-new"))),
    ]
    for state, reported in reports:
        subprocess.run(status_line["command"], shell=True, check=True, text=True,
                       input=json.dumps({"agent_state": state, "conversation_id": reported,
                                         "cwd": os.getcwd()}))
print(json.dumps(args))
""")
    fake_agy.chmod(0o755)
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "PYTHONPATH": os.pathsep.join(str(root / part) for part in ("", "src", "container/lib")),
        "WOLTSPACE_WOLTS_DIR": str(wolts),
        "WOLTSPACE_DIR": str(root),
        "WOLTSPACE_PYTHON": sys.executable,
        "WOLTSPACE_ISOLATION": "host",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(wolts))
    monkeypatch.setenv("WOLTSPACE_ISOLATION", "host")
    monkeypatch.setattr(sessions, "RUN_SESSION_SCRIPT", root / "container/bin/run-session.sh")

    request = {"wolt": "maple", "workdir": str(repo), "prompt": "Keep this '$literal'\nand this line."}
    names = []
    for _ in range(2):
        response = asyncio.run(_request("POST", "/sessions/new/lodge", json=request))
        assert response.status_code == 200, response.text
        assert response.json()["execution_policy"]["mode"] == "prompt"
        names.append(response.json()["name"])

    def run_launch(command):
        wrapper, name, *arguments = shlex.split(command)
        assert wrapper == str(sessions.RUN_SESSION_SCRIPT)
        mode = "resume" if arguments and arguments[0] == "--resume" else "spawn"
        prompt = arguments[-1]
        engine = sessions.prepare_session_command(name, mode, prompt)
        result = subprocess.run(
            shlex.split(engine),
            env={**env, "WOLTSPACE_WOLT_SESSION": name, "WOLTSPACE_WOLT_HOME": str(home)},
            cwd=repo, text=True, capture_output=True, check=True, timeout=20,
        )
        return json.loads(result.stdout)

    with ThreadPoolExecutor(max_workers=2) as pool:
        launches = list(pool.map(run_launch, [command for _, _, command in fake_runtime.spawns]))

    registry = sessions.SessionRegistry()
    for name, args in zip(names, launches, strict=True):
        assert args[:2] == ["--add-dir", str(home)]
        assert "--conversation" not in args
        assert args[args.index("--model") + 1] == "gemini-3.8-flash-medium"
        settings = json.loads(args[args.index("--settings") + 1])
        assert (settings["agentMode"], settings["toolPermission"]) == ("", "request-review")
        assert "-i" not in args
        data = registry.get(name, check_alive=False)
        assert request["prompt"] in data["pending_boot_prompt"]
        assert "/woltspace-start-chat lodge maple" in data["pending_boot_prompt"]
        assert data["harness_session_id"] == str(uuid5(NAMESPACE_URL, name))
        fake_runtime.feed_capture(["Accessing workspace", ">\n? for shortcuts"])
        with monkeypatch.context() as clock:
            clock.setattr(sessions.time, "sleep", lambda _: None)
            assert sessions.deliver_boot_prompt(name)
        assert request["prompt"] in fake_runtime.last_paste[1]

    # A dead/finished TUI relaunches the same harness even if the wolt switches.
    (home / "wolt" / "wolt.json").write_text('{"name":"maple","harness":"claude"}')
    monkeypatch.setattr(sessions, "_tmux_alive", lambda *args: False)
    response = asyncio.run(_request("POST", f"/sessions/{names[0]}/resume", json={"prompt": "follow up"}))
    assert response.status_code == 200, response.text
    args = run_launch(fake_runtime.last_spawn[2])
    assert args[args.index("--conversation") + 1] == str(uuid5(NAMESPACE_URL, names[0]))
    assert registry.get(names[0], check_alive=False)["pending_boot_prompt"] == "follow up"
    assert "-i" not in args and "--continue" not in args
    assert "statusLine" not in json.loads(args[args.index("--settings") + 1])


def test_antigravity_callback_ignores_an_id_for_a_different_cwd_or_engine(tmp_path, monkeypatch):
    from uuid import uuid4

    import sessions
    from antigravity_status import capture_id

    wolts, home, repo = _layout(tmp_path, monkeypatch)
    registry = sessions.SessionRegistry(wolts)
    registry.create("antigravity-session", wolt="maple", dir=str(repo), harness="antigravity")
    registry.create("claude-session", wolt="maple", dir=str(repo), harness="claude")
    conversation = str(uuid4())
    capture_id("antigravity-session", {"conversation_id": conversation, "cwd": str(home)})
    capture_id("antigravity-session", {"conversation_id": "wrong", "cwd": str(repo)})
    capture_id("claude-session", {"conversation_id": conversation, "cwd": str(repo)})
    for name in ("antigravity-session", "claude-session"):
        assert not registry.get(name, check_alive=False).get("harness_session_id")

    capture_id("antigravity-session", {"conversation_id": conversation, "cwd": str(repo)})
    registry.create("another-session", wolt="maple", dir=str(repo), harness="antigravity")
    capture_id("another-session", {"conversation_id": conversation, "cwd": str(repo)})
    assert not registry.get("another-session", check_alive=False).get("harness_session_id")
