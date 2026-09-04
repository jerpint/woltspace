"""Named-session runtime unit tests plus one harmless host-tmux integration."""

import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))

from runtime_context import RuntimeContext
from session_runtime import RuntimeHandle, TmuxSessionRuntime

from conftest import requires_tmux


class FakeRunner:
    def __init__(self, outputs=None):
        self.outputs = list(outputs or [])
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        stdout = self.outputs.pop(0) if self.outputs else ""
        return SimpleNamespace(stdout=stdout, returncode=0)


def context(tmp_path, tmux_bin="tmux"):
    return RuntimeContext(
        install_root=tmp_path,
        wolts_root=tmp_path / "wolts",
        run_session_script=tmp_path / "run-session.sh",
        tmux_bin=tmux_bin,
    )


class TestRuntimeContext:
    def test_from_env_is_injectable(self, tmp_path):
        ctx = RuntimeContext.from_env(
            {"WOLTS_DIR": str(tmp_path / "data"), "WOLTSPACE_TMUX_BIN": "tmux-test"},
            install_root=tmp_path / "install",
        )
        assert ctx.install_root == tmp_path / "install"
        assert ctx.wolts_root == tmp_path / "data"
        assert ctx.tmux_bin == "tmux-test"
        assert ctx.run_session_script == tmp_path / "install" / "container" / "bin" / "run-session.sh"


class TestRuntimeHandle:
    def test_round_trips_registry_shape(self):
        handle = RuntimeHandle("wolt-mossy-log-aabbcc", "wolt-mossy-log-aabbcc", "%42")
        restored = RuntimeHandle.from_record({"name": handle.woltspace_session_id, "runtime": handle.to_record()})
        assert restored == handle

    def test_old_record_falls_back_to_exact_named_session(self):
        handle = RuntimeHandle.from_record({"name": "legacy-session"})
        assert handle.woltspace_session_id == "legacy-session"
        assert handle.tmux_session_name == "legacy-session"
        assert handle.pane_id == ""


class TestTmuxSessionRuntime:
    def test_spawn_returns_exact_pane_handle(self, tmp_path):
        runner = FakeRunner(["%17\n"])
        runtime = TmuxSessionRuntime(context(tmp_path), runner=runner)

        handle = runtime.spawn("named-session", str(tmp_path), "cat")

        assert handle == RuntimeHandle("named-session", "named-session", "%17")
        command = runner.calls[0][0]
        assert command[:6] == ["tmux", "new-session", "-d", "-P", "-F", "#{pane_id}"]
        assert command[-1] == "cat"

    def test_liveness_checks_persisted_pane_in_named_session(self, tmp_path):
        runner = FakeRunner(["named-session\t%17\t123\t1\nnamed-session\t%18\t456\t0\n"])
        runtime = TmuxSessionRuntime(context(tmp_path), runner=runner)

        assert runtime.is_alive(RuntimeHandle("named-session", "named-session", "%18")) is True
        command = runner.calls[0][0]
        assert command[1:5] == ["list-panes", "-s", "-t", "=named-session"]

    def test_liveness_rejects_missing_persisted_pane(self, tmp_path):
        runner = FakeRunner(["named-session\t%17\t123\t1\n"])
        runtime = TmuxSessionRuntime(context(tmp_path), runner=runner)

        assert runtime.is_alive(RuntimeHandle("named-session", "named-session", "%99")) is False

    def test_paste_targets_pane_id_atomically(self, tmp_path):
        runner = FakeRunner()
        sleeps = []
        runtime = TmuxSessionRuntime(context(tmp_path), runner=runner, sleeper=sleeps.append)

        runtime.paste(RuntimeHandle("named-session", "named-session", "%17"), "hello\nworld", settle=0.5)

        commands = [call[0] for call in runner.calls]
        assert commands[0] == ["tmux", "send-keys", "-t", "%17", "-X", "cancel"]
        assert commands[1] == ["tmux", "set-buffer", "-b", "paste-named-session", "hello\nworld"]
        assert commands[2] == ["tmux", "paste-buffer", "-b", "paste-named-session", "-d", "-t", "%17"]
        assert commands[3] == ["tmux", "send-keys", "-t", "%17", "Enter"]
        assert sleeps == [0.5]

    def test_legacy_paste_targets_named_session_without_discovery(self, tmp_path):
        """A record with no persisted pane targets the bare session name.

        tmux accepts the '=' exact-match prefix for a target-session but
        rejects it for a target-pane ("can't find pane: =legacy"), so the
        pre-runtime-handle fallback must pass the name unprefixed — the
        pre-refactor behavior — and must still not discover panes.
        """
        runner = FakeRunner()
        runtime = TmuxSessionRuntime(context(tmp_path), runner=runner)

        runtime.paste(RuntimeHandle("legacy", "legacy"), "hello")

        commands = [call[0] for call in runner.calls]
        assert all("list-panes" not in command for command in commands)
        assert commands[0][3] == "legacy"
        assert commands[2][-1] == "legacy"
        assert commands[3] == ["tmux", "send-keys", "-t", "legacy", "Enter"]

    def test_session_names_come_from_server_wide_pane_snapshot(self, tmp_path):
        runner = FakeRunner(["main\t%1\t10\t1\nnamed-a\t%2\t20\t1\nnamed-a\t%3\t30\t0\n"])
        runtime = TmuxSessionRuntime(context(tmp_path), runner=runner)

        assert runtime.list_session_names() == {"named-a"}
        assert runner.calls[0][0][1:3] == ["list-panes", "-a"]


def test_start_session_persists_runtime_handle(tmp_path, monkeypatch):
    import paths
    import sessions
    import sites

    monkeypatch.setattr(sessions, "WOLTS_DIR", tmp_path)
    monkeypatch.setattr(sessions, "RUN_SESSION_SCRIPT", Path("/bin/true"))
    monkeypatch.setattr(sites, "WOLTS_DIR", tmp_path)
    monkeypatch.setattr(paths, "WOLTS_DIR", tmp_path)

    wolt_dir = tmp_path / "testwolt" / "wolt"
    (wolt_dir / "site").mkdir(parents=True)
    (wolt_dir / "site" / "index.html").write_text("<h1>test</h1>")
    (wolt_dir / "wolt.json").write_text(json.dumps({"name": "testwolt", "type": "raccoon"}))

    monkeypatch.setattr(
        sessions.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout="%42\n", returncode=0),
    )

    result = sessions.start_session(wolt="testwolt", prompt="hello")
    stored = sessions.SessionRegistry(tmp_path).get(result["name"], check_alive=False)

    assert stored["runtime"] == {
        "woltspace_session_id": result["name"],
        "tmux_session_name": result["name"],
        "pane_id": "%42",
        "kind": "tmux",
    }


@requires_tmux
def test_host_tmux_named_session_round_trip(tmp_path):
    runtime = TmuxSessionRuntime(context(tmp_path))
    name = f"test-runtime-{uuid.uuid4().hex[:10]}"
    handle = None
    try:
        handle = runtime.spawn(name, str(tmp_path), "cat")
        assert handle.pane_id.startswith("%")
        assert runtime.is_alive(handle)

        marker = f"marker-{uuid.uuid4().hex}"
        runtime.paste(handle, marker)
        deadline = time.time() + 3
        captured = ""
        while time.time() < deadline:
            captured = runtime.capture(handle, start="-20")
            if marker in captured:
                break
            time.sleep(0.05)
        assert marker in captured
    finally:
        if handle is not None:
            runtime.stop(handle)
        else:
            subprocess.run(["tmux", "kill-session", "-t", f"={name}"], capture_output=True)

    assert runtime.is_alive(RuntimeHandle(name, name)) is False


@requires_tmux
def test_host_tmux_legacy_record_round_trip(tmp_path):
    """A session record predating runtime handles must still be reachable.

    Every session in an existing registry has no `runtime` field, so its
    handle carries no pane_id. Real tmux rejects the '=' exact-match prefix
    on a target-pane, so that fallback has to address the bare session name
    or paste/capture break for every session a user already has.
    """
    runtime = TmuxSessionRuntime(context(tmp_path))
    name = f"test-legacy-{uuid.uuid4().hex[:10]}"
    legacy = RuntimeHandle.from_record({"name": name})
    assert legacy.pane_id == ""

    subprocess.run(["tmux", "new-session", "-d", "-s", name, "cat"], check=True)
    try:
        assert runtime.is_alive(legacy)

        marker = f"marker-{uuid.uuid4().hex}"
        runtime.paste(legacy, marker)
        deadline = time.time() + 3
        captured = ""
        while time.time() < deadline:
            captured = runtime.capture(legacy, start="-20")
            if marker in captured:
                break
            time.sleep(0.05)
        assert marker in captured
    finally:
        runtime.stop(legacy)

    assert runtime.is_alive(legacy) is False
