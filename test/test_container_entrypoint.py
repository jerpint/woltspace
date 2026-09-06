"""Container boot, as the CLI runs it — no root, no docker, no tmux.

The boot that used to be three bash/python files is one module now, so the
things bash could only be checked by eye — which variables every child
inherits, which greeting the human lands on, what the first-run sweep touches —
are unit-testable. Nothing here may need privileges: every path is a tmp dir.

Usage: uv run pytest test/test_container_entrypoint.py -v
"""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from woltspace import container_entrypoint as boot  # noqa: E402


# ---------------------------------------------------------------------------
# Environment assembly — what replaced the sourceable env file
# ---------------------------------------------------------------------------

class TestEnvironmentAssembly:
    def _env(self, tmp_path, **overrides):
        kwargs = dict(
            wolt_name="mywolt",
            wolt_dir=tmp_path / "wolts" / "mywolt",
            wolts_dir=tmp_path / "wolts",
            woltspace_dir=tmp_path / "bundle",
            dev_mode=False,
            env={"PATH": "/usr/bin", "PYTHONPATH": ""},
        )
        kwargs.update(overrides)
        return boot.build_environment(**kwargs)

    def test_carries_every_derived_value_bash_used_to_source(self, tmp_path):
        env = self._env(tmp_path)

        assert env["WOLT_NAME"] == "mywolt"
        assert env["WOLT_DIR"] == str(tmp_path / "wolts" / "mywolt")
        assert env["WOLTS_DIR"] == str(tmp_path / "wolts")
        assert env["DEV_MODE"] == "false"
        assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
        assert env["LANG"] == "C.UTF-8"

    def test_declares_the_two_facts_only_this_process_knows(self, tmp_path):
        """A stray `woltspace serve` inherits everything else — not these.

        The control plane refuses to act as owner of the data root, the tunnel
        or the bot token unless it sees WOLTSPACE_ENTRYPOINT.
        """
        env = self._env(tmp_path)

        assert env["WOLTSPACE_ENTRYPOINT"] == "1"
        assert env["WOLTSPACE_ISOLATION"] == "external"

    def test_prepends_the_bundle_to_path_and_pythonpath(self, tmp_path):
        bundle = tmp_path / "bundle"
        env = self._env(tmp_path, env={"PATH": "/usr/bin", "PYTHONPATH": "/seed"})

        assert env["PATH"] == f"{bundle}/container/bin:/usr/bin"
        assert env["PYTHONPATH"] == f"{bundle}/container/lib:/seed"

    def test_an_absent_pythonpath_leaves_a_bare_prepend(self, tmp_path):
        env = self._env(tmp_path, env={})

        assert env["PYTHONPATH"] == f"{tmp_path / 'bundle'}/container/lib:"

    def test_dev_mode_is_declared_not_inferred(self, tmp_path):
        assert self._env(tmp_path, dev_mode=True)["DEV_MODE"] == "true"
        assert boot.is_truthy("YES") and boot.is_truthy("1") and boot.is_truthy("on")
        assert not boot.is_truthy("") and not boot.is_truthy(None)
        assert not boot.is_truthy("false")

    def test_bot_modules_fall_back_to_the_platform_adapters(self, tmp_path):
        env = self._env(tmp_path)
        bundle = tmp_path / "bundle"

        assert env["TELEGRAM_BOT_MODULE"] == "bot.telegram_adapter"
        assert env["TELEGRAM_BOT_DIR"] == str(bundle / "container")
        assert env["SLACK_BOT_MODULE"] == "bot.slack_adapter"
        assert env["SLACK_BOT_DIR"] == str(bundle / "container")

    def test_a_wolt_owned_adapter_wins(self, tmp_path):
        wolt_dir = tmp_path / "wolts" / "mywolt"
        (wolt_dir / "wolt" / "bot").mkdir(parents=True)
        (wolt_dir / "wolt" / "bot" / "telegram_adapter.py").write_text("# mine\n")

        env = self._env(tmp_path)

        assert env["TELEGRAM_BOT_DIR"] == str(wolt_dir)
        assert env["TELEGRAM_BOT_MODULE"] == "wolt.bot.telegram_adapter"
        # Slack has no override, so it still points at the platform's copy
        assert env["SLACK_BOT_MODULE"] == "bot.slack_adapter"

    def test_nothing_is_written_to_the_real_environment(self, tmp_path):
        import os

        self._env(tmp_path)

        assert "WOLTSPACE_ENTRYPOINT" not in os.environ or \
            os.environ.get("WOLTSPACE_ENTRYPOINT") != "1"


# ---------------------------------------------------------------------------
# The greeting the human lands on
# ---------------------------------------------------------------------------

class _TmuxCalls(list):
    """The recorded commands, with a dict of verbs that should fail."""

    def __init__(self):
        super().__init__()
        self.failures: dict[tuple, int] = {}


@pytest.fixture
def fake_tmux(tmp_path, monkeypatch):
    """Record every tmux invocation instead of running one.

    `calls.failures[("tmux", verb, ...)] = code` makes one tmux verb fail, so a
    test can prove which failures boot is supposed to survive.
    """
    calls = _TmuxCalls()
    failures = calls.failures

    import subprocess

    def run(command, **kwargs):
        calls.append(list(command))
        code = failures.get(tuple(command[:3]), 0)
        if code and kwargs.get("check"):
            raise subprocess.CalledProcessError(code, command)
        return subprocess.CompletedProcess(command, code, b"", b"")

    monkeypatch.setattr(boot.subprocess, "run", run)
    monkeypatch.setattr(boot, "HOME", tmp_path / "home")
    (tmp_path / "home" / ".claude").mkdir(parents=True)
    return calls


def _sent(calls):
    return [c[4] for c in calls if c[:2] == ["tmux", "send-keys"]]


class TestGreetingBranches:
    def test_no_auth_boots_onboard_mode(self, tmp_path, fake_tmux, capsys):
        boot.open_tmux_window("mywolt", tmp_path / "wolt", tmp_path / "wolts")

        assert _sent(fake_tmux) == ["wclaude /login"]
        assert "onboard mode: has_auth=false wolt_name=mywolt" in capsys.readouterr().out

    def test_no_wolt_boots_onboard_mode(self, tmp_path, fake_tmux, capsys):
        (tmp_path / "home" / ".claude" / ".credentials.json").write_text("{}")

        boot.open_tmux_window("", tmp_path / "wolt", tmp_path / "wolts")

        assert _sent(fake_tmux) == ["wclaude /login"]
        assert "wolt_name=<none>" in capsys.readouterr().out

    def test_first_run_launches_creation_and_clears_the_marker(self, tmp_path, fake_tmux):
        claude = tmp_path / "home" / ".claude"
        (claude / ".credentials.json").write_text("{}")
        (claude / ".first-run").touch()
        wolt_dir = tmp_path / "wolts" / "mywolt"
        wolt_dir.mkdir(parents=True)

        boot.open_tmux_window("mywolt", wolt_dir, tmp_path / "wolts")

        assert _sent(fake_tmux) == [
            "export WOLT_SESSION=main && wclaude --dangerously-skip-permissions "
            "/woltspace:create-wolt"
        ]
        assert not (claude / ".first-run").exists()

    def test_a_normal_boot_greets_the_wolt(self, tmp_path, fake_tmux):
        (tmp_path / "home" / ".claude" / ".credentials.json").write_text("{}")

        boot.open_tmux_window("mywolt", tmp_path / "wolt", tmp_path / "wolts")

        assert _sent(fake_tmux) == [
            'wclaude --dangerously-skip-permissions "hey mywolt"'
        ]

    def test_an_existing_main_session_is_not_an_error(self, tmp_path, fake_tmux):
        """`2>/dev/null || true` — a restart finds `main` already there."""
        (tmp_path / "home" / ".claude" / ".credentials.json").write_text("{}")
        fake_tmux.failures[("tmux", "-u", "new-session")] = 1

        boot.open_tmux_window("mywolt", tmp_path / "wolt", tmp_path / "wolts")

        assert _sent(fake_tmux) == ['wclaude --dangerously-skip-permissions "hey mywolt"']

    def test_an_unreachable_tmux_kills_the_boot(self, tmp_path, fake_tmux):
        """Under `set -e` everything after the create was fatal. Still is.

        A swallowed failure leaves a healthy-looking API with no window behind
        it — and on first run the marker is already spent, so the creation
        greeting would never be offered again.
        """
        (tmp_path / "home" / ".claude" / ".credentials.json").write_text("{}")
        fake_tmux.failures[("tmux", "set", "-g")] = 1

        with pytest.raises(subprocess.CalledProcessError):
            boot.open_tmux_window("mywolt", tmp_path / "wolt", tmp_path / "wolts")

    def test_a_greeting_that_never_lands_kills_the_boot(self, tmp_path, fake_tmux):
        (tmp_path / "home" / ".claude" / ".credentials.json").write_text("{}")
        fake_tmux.failures[("tmux", "send-keys", "-t")] = 1

        with pytest.raises(subprocess.CalledProcessError):
            boot.open_tmux_window("mywolt", tmp_path / "wolt", tmp_path / "wolts")

    def test_the_window_opens_in_the_wolt_dir_with_mouse_on(self, tmp_path, fake_tmux):
        (tmp_path / "home" / ".claude" / ".credentials.json").write_text("{}")
        wolt_dir = tmp_path / "wolts" / "mywolt"

        boot.open_tmux_window("mywolt", wolt_dir, tmp_path / "wolts")

        assert fake_tmux[0] == [
            "tmux", "-u", "new-session", "-d", "-s", "main", "-c", str(wolt_dir),
        ]
        assert fake_tmux[1] == ["tmux", "set", "-g", "mouse", "on"]


# ---------------------------------------------------------------------------
# First-run chores
# ---------------------------------------------------------------------------

class TestFirstRunSweep:
    def test_clears_node_modules_two_levels_deep(self, tmp_path, capsys):
        wolts = tmp_path / "wolts"
        (wolts / "apps" / "myapp" / "node_modules" / "left").mkdir(parents=True)
        (wolts / "apps" / "node_modules").mkdir()
        (wolts / "projects" / "thing" / "node_modules").mkdir(parents=True)
        keep = wolts / "apps" / "myapp" / "src"
        keep.mkdir()
        # Three levels down is out of reach, exactly as find -maxdepth 2 was
        deep = wolts / "apps" / "myapp" / "src" / "node_modules"
        deep.mkdir()

        boot.sweep_node_modules(wolts)

        assert not (wolts / "apps" / "myapp" / "node_modules").exists()
        assert not (wolts / "apps" / "node_modules").exists()
        assert not (wolts / "projects" / "thing" / "node_modules").exists()
        assert deep.exists()
        assert "fresh container: clearing node_modules" in capsys.readouterr().out

    def test_a_lodge_without_apps_is_not_an_error(self, tmp_path):
        boot.sweep_node_modules(tmp_path / "wolts")  # must not raise

    def test_preloads_the_viewport_with_the_wolt_site(self, tmp_path):
        url = boot.preload_viewport("mywolt", tmp_path / ".state")

        payload = json.loads((tmp_path / ".state" / "current-url-main.json").read_text())
        assert url == "/wolt/mywolt/site/"
        assert payload["url"] == "/wolt/mywolt/site/"
        assert payload["port"] == 7777
        assert isinstance(payload["updated"], int)


# ---------------------------------------------------------------------------
# Slack — the one process boot still starts by hand
# ---------------------------------------------------------------------------

class TestSlackBot:
    BASE = {
        "ENABLE_SLACK_BOT": "true",
        "SLACK_BOT_TOKEN": "xoxb-token",
        "SLACK_APP_TOKEN": "xapp-token",
        "SLACK_BOT_DIR": "/bundle/container",
        "SLACK_BOT_MODULE": "bot.slack_adapter",
        "DEV_MODE": "false",
        "PYTHONPATH": "/bundle/container/lib:",
    }

    def _launch(self, env):
        with patch.object(boot.subprocess, "Popen") as popen:
            boot.start_slack_bot(env)
        return popen

    def test_not_started_without_both_tokens(self):
        for missing in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"):
            env = dict(self.BASE, **{missing: ""})
            assert self._launch(env).call_count == 0

    def test_not_started_unless_enabled(self):
        assert self._launch(dict(self.BASE, ENABLE_SLACK_BOT="false")).call_count == 0

    def test_runs_on_the_installed_interpreter_detached(self):
        popen = self._launch(dict(self.BASE))

        args, kwargs = popen.call_args
        assert args[0] == ["woltspace-python", "-m", "bot.slack_adapter"]
        assert kwargs["cwd"] == "/bundle/container"
        assert kwargs["start_new_session"] is True
        assert kwargs["env"]["BOT_ADAPTER"] == "slack"
        assert kwargs["env"]["PYTHONPATH"] == "/bundle/container:/bundle/container/lib:"

    def test_a_bot_that_cannot_start_is_a_warning_not_a_dead_colony(self, capsys):
        """Bash backgrounded this; the failure cost one line and nothing else."""
        with patch.object(boot.subprocess, "Popen",
                          side_effect=FileNotFoundError(2, "no woltspace-python")):
            assert boot.start_slack_bot(dict(self.BASE)) is None

        assert "slack bot failed to start:" in capsys.readouterr().out

    def test_dev_mode_wraps_it_in_watchfiles(self):
        popen = self._launch(dict(self.BASE, DEV_MODE="true"))

        assert popen.call_args.args[0] == [
            "woltspace-python", "-m", "watchfiles", "--filter", "python",
            "python -m bot.slack_adapter", "bot/",
        ]


# ---------------------------------------------------------------------------
# Tunnel reporting
# ---------------------------------------------------------------------------

class TestTunnelReport:
    def test_disabled_says_so_and_starts_no_thread(self, tmp_path, capsys):
        thread = boot.start_tunnel_report(tmp_path, {"WOLTSPACE_PUBLIC_TUNNEL": "false"})

        assert thread is None
        assert "tunnel disabled — access via http://localhost:7777" in capsys.readouterr().out

    def test_an_empty_value_still_means_enabled(self, tmp_path, monkeypatch):
        """`${WOLTSPACE_PUBLIC_TUNNEL:-true}` treated empty as unset."""
        monkeypatch.setattr(boot, "report_tunnel_url", lambda wolts_dir: None)

        thread = boot.start_tunnel_report(tmp_path, {"WOLTSPACE_PUBLIC_TUNNEL": ""})

        assert thread is not None
        thread.join(timeout=5)

    def test_reports_the_url_once_the_state_file_lands(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(boot, "TUNNEL_POLL_INTERVAL", 0)
        state = tmp_path / ".space" / "platform"
        state.mkdir(parents=True)
        (state / "tunnel.json").write_text(json.dumps({"url": "https://x.trycloudflare.com"}))

        boot.report_tunnel_url(tmp_path)

        out = capsys.readouterr().out
        assert "waiting for tunnel..." in out
        assert "tunnel ready: https://x.trycloudflare.com" in out

    def test_gives_up_loudly_but_never_blocks_the_boot(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(boot, "TUNNEL_POLL_INTERVAL", 0)
        monkeypatch.setattr(boot, "TUNNEL_POLL_ATTEMPTS", 2)

        boot.report_tunnel_url(tmp_path)

        assert "warning: tunnel URL not available yet" in capsys.readouterr().out

    def test_the_report_thread_never_holds_the_process_open(self, tmp_path, monkeypatch):
        monkeypatch.setattr(boot, "report_tunnel_url", lambda wolts_dir: None)

        thread = boot.start_tunnel_report(tmp_path, {})

        assert thread.daemon is True
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Phase dispatch
# ---------------------------------------------------------------------------

class TestPhaseDispatch:
    @pytest.fixture(autouse=True)
    def _keep_this_process_home(self, monkeypatch):
        """The root phase rewrites HOME; never leak /home/node into the suite."""
        monkeypatch.setenv("HOME", "/root")

    def test_root_does_the_uid_fixup_then_re_execs_itself_as_node(self, monkeypatch):
        commands = []
        monkeypatch.setattr(boot.os, "getuid", lambda: 0)
        monkeypatch.setattr(boot.subprocess, "run",
                            lambda command, **kw: commands.append(list(command)))
        monkeypatch.setenv("HOST_UID", "501")
        monkeypatch.setenv("HOST_GID", "20")
        execs = []
        monkeypatch.setattr(boot.os, "execvp", lambda f, a: execs.append((f, a)))

        boot.main()

        assert commands[0] == ["groupmod", "-o", "-g", "20", "node"]
        assert commands[1] == ["usermod", "-o", "-u", "501", "-g", "20", "node"]
        assert commands[2] == ["chown", "node:node", "/workspace"]
        assert commands[3] == ["chown", "-R", "node:node", "/home/node"]
        # ...and the bundle, which boot writes the derived worktui skill into
        assert commands[4][:3] == ["chown", "-R", "node:node"]
        assert execs == [("gosu", [
            "gosu", "node", "/usr/local/bin/woltspace", "container-entrypoint",
        ])]

    def test_root_defaults_to_the_first_linux_user(self, monkeypatch):
        commands = []
        monkeypatch.setattr(boot.os, "getuid", lambda: 0)
        monkeypatch.setattr(boot.subprocess, "run",
                            lambda command, **kw: commands.append(list(command)))
        monkeypatch.delenv("HOST_UID", raising=False)
        monkeypatch.delenv("HOST_GID", raising=False)
        monkeypatch.setattr(boot.os, "execvp", lambda f, a: None)

        boot.main()

        assert commands[1] == ["usermod", "-o", "-u", "1000", "-g", "1000", "node"]

    def test_root_names_nodes_home_before_dropping_privileges(self, monkeypatch):
        """gosu leaves the environment alone; root's HOME is /root."""
        monkeypatch.setattr(boot.os, "getuid", lambda: 0)
        monkeypatch.setattr(boot.subprocess, "run", lambda command, **kw: None)
        monkeypatch.setattr(boot.os, "execvp", lambda f, a: None)

        boot.main()

        assert boot.os.environ["HOME"] == "/home/node"

    def test_non_root_runs_the_node_phase(self, monkeypatch):
        monkeypatch.setattr(boot.os, "getuid", lambda: 1000)
        monkeypatch.setattr(boot, "run_node_phase", lambda: 7)

        assert boot.main() == 7
