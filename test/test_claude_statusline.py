import json
import shlex
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

import usage_support  # noqa: F401
import claude_statusline
from harnesses import build_command
from usage_collector import claude_limits, claude_limits_path


@pytest.mark.parametrize("isolation", ["host", "external"])
@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("mode", ["spawn", "resume"])
def test_overlay_keeps_hooks_and_custom_settings(isolation, native, mode, tmp_path):
    home, wolt, project, managed = [tmp_path / name for name in ("home", "wolt", "project", "managed")]
    for folder in (home, wolt, project):
        (folder / ".claude").mkdir(parents=True)
    env = {"HOME": str(home), "WOLTSPACE_WOLT_HOME": str(wolt), "WOLTSPACE_ISOLATION": isolation}
    settings = tmp_path / "native.json" if native else None
    hooks = {"hooks": {"Stop": []}} if native else {}
    if settings:
        settings.write_text(json.dumps(hooks))
    call = lambda: claude_statusline.overlay(tmp_path, project, settings, environ=env, managed=managed)
    overlay = call()
    assert json.loads(overlay).get("hooks") == hooks.get("hooks")
    locator = {"addr": str(tmp_path), "attempt": "one", "kind": "claude"} if native else None
    argv = shlex.split(build_command("claude", mode, native=locator, claude_settings=overlay))
    assert argv.count("--settings") == 1
    assert json.loads(argv[argv.index("--settings") + 1])["statusLine"]["type"] == "command"
    user = (home if isolation == "host" else wolt) / ".claude/settings.json"
    user.write_text('{"statusLine":null}')
    assert call() is None
    user.unlink()
    local = project / ".claude/settings.local.json"
    local.write_text("{")
    assert call() is None
    local.unlink()
    (tmp_path / "woltspace.json").write_text('{"usage":{"claude_limits":{"enabled":false}}}')
    assert call() is None


def test_writer_and_reader_keep_only_valid_windows_and_latest_timestamp(tmp_path):
    helper = [sys.executable, claude_statusline.__file__, str(tmp_path)]
    data = {"ignored": "private", "rate_limits": {
        "five_hour": {"used_percentage": 12, "resets_at": 123, "unknown": "private"},
        "seven_day": {"used_percentage": 101, "resets_at": 456},
    }}
    snapshot = claude_limits_path(tmp_path)
    for raw in (json.dumps(data).encode(), b"", b"not json", b"x" * 131073):
        result = subprocess.run(helper, input=raw, capture_output=True)
        assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")
    contents = snapshot.read_bytes()
    assert b"private" not in contents and b"seven_day" not in contents
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600
    assert stat.S_IMODE(snapshot.parent.stat().st_mode) == 0o700
    observed = datetime.fromisoformat(claude_limits(snapshot)["observed_at"])
    assert observed.tzinfo == timezone.utc
    data["rate_limits"]["five_hour"]["used_percentage"] = 50
    for when in (datetime(2000, 1, 1, tzinfo=timezone.utc), observed):
        claude_statusline.publish(tmp_path, when, data)
        assert snapshot.read_bytes() == contents
    (tmp_path / "woltspace.json").write_text('{"usage":{"claude_limits":{"enabled":false}}}')
    subprocess.run(helper, input=json.dumps(data).encode(), check=True)
    assert snapshot.read_bytes() == contents
    bad_root = tmp_path / "ordinary-file"
    bad_root.touch()
    result = subprocess.run([*helper[:2], str(bad_root)], input=json.dumps(data).encode(), capture_output=True)
    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")


@pytest.mark.parametrize("hints", [False, True])
def test_session_launch_command_delivers_overlay_to_claude(tmp_path, monkeypatch, hints):
    import os
    from pathlib import Path

    import native_adapters
    import paths
    import sessions
    from usage_support import wolt

    root = tmp_path / "wolts"
    owner = wolt(tmp_path, "wolt-a")
    home = tmp_path / "home"
    settings = home / ".claude/settings.json"
    settings.parent.mkdir(parents=True)
    (owner / ".claude").mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "claude"
    stub.write_text(f"#!{sys.executable}\nimport sys, json\nprint(json.dumps(sys.argv[1:]))\n")
    stub.chmod(0o700)
    wrappers = Path(claude_statusline.__file__).parents[1] / "bin"
    monkeypatch.setenv("PATH", os.pathsep.join(map(str, (bin_dir, wrappers, os.environ["PATH"]))))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WOLTSPACE_ISOLATION", "host")
    monkeypatch.setenv("WOLTSPACE_WOLT_HOME", str(owner))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(paths, "WOLTS_DIR", root)
    monkeypatch.setattr(sessions, "WOLTS_DIR", root)
    monkeypatch.setitem(claude_statusline.MANAGED, sys.platform, tmp_path / "managed")
    registry = sessions.SessionRegistry(root)
    name = "wolt-a-session"
    registry.create(name, wolt="wolt-a", harness="claude", dir=str(owner))
    locator = native_adapters.prepare(root, name, "claude", "one") if hints else None
    if locator:
        registry.update(name, native=locator)

    for content, opt_out in ((None, False), ('{"statusLine":{"type":"command","command":"custom"}}', False),
                             ('{"statusLine":null}', False), ("{", False), (None, True)):
        if content is None:
            settings.unlink(missing_ok=True)
        else:
            settings.write_text(content)
        (root / "woltspace.json").write_text(json.dumps({"usage": {"claude_limits": {"enabled": not opt_out}}}))
        for mode in ("spawn", "resume"):
            command = sessions.prepare_session_command(name, mode, "test prompt")
            result = subprocess.run(["bash", "-c", command], cwd=owner,
                                    capture_output=True, text=True)
            assert result.returncode == 0, result.stderr
            argv = json.loads(result.stdout)
            if content is None and not opt_out:
                assert argv.count("--settings") == 1
                overlay = json.loads(argv[argv.index("--settings") + 1])
                assert claude_statusline.__file__ in shlex.split(overlay["statusLine"]["command"])
                assert ("hooks" in overlay) == hints
            elif hints:
                overlay_path = Path(argv[argv.index("--settings") + 1])
                assert "statusLine" not in json.loads(overlay_path.read_text())
            else:
                assert "--settings" not in argv
            assert ("--resume" in argv) == (mode == "resume")
        if content is not None:
            assert settings.read_text() == content


def test_a_far_future_snapshot_can_be_replaced_after_a_clock_step(tmp_path):
    now = datetime.now(timezone.utc)

    def publish(at, percent):
        claude_statusline.publish(tmp_path, at, {"rate_limits": {
            "five_hour": {"used_percentage": percent, "resets_at": 123},
        }})

    snapshot = claude_limits_path(tmp_path)
    publish(now + timedelta(minutes=10), 12)
    publish(now, 25)
    assert claude_limits(snapshot)["observed_at"] == now.isoformat()
    publish(now - timedelta(minutes=1), 40)
    assert claude_limits(snapshot)["windows"]["five_hour"]["used_percent"] == 25
