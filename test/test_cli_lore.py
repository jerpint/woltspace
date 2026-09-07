"""The lodge's voice, and the promises that keep it safe to speak.

Two things are under test here, and they pull in opposite directions:

* the CLI says the lore lines the launcher said, in the colours it used;
* nothing a person or an agent needs is lost to the saying of it — the human
  render keeps every fact worth reading, and `--json` keeps every diagnostic.

Every test pins the scratch colony through `_pin_scratch_colony`, which sets
*both* spellings of every path and port variable. Nothing here may resolve
against a live colony, and nothing here may be decided by what an earlier test
in the same process happened to leave in `os.environ`.

Usage: uv run pytest test/test_cli_lore.py -v
"""

import json
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from woltspace import lore  # noqa: E402
from woltspace.cli import main as cli_main  # noqa: E402
from woltspace.layout import RuntimeLayout  # noqa: E402
from woltspace.lifecycle import read_tunnel_url, tunnel_report, tunnel_settings  # noqa: E402


#: Every spelling of "where the wolts live", and every spelling of "which
#: port". A test that pins only one of a pair is a test that passes alone and
#: fails in a suite: an earlier test writing the *other* spelling into the real
#: `os.environ` shadows it, and which one wins depends on resolution order this
#: file has no business knowing. So pin both, always, in both directions.
WOLTS_DIR_NAMES = ("WOLTSPACE_WOLTS_DIR", "WOLTS_DIR")
PORT_NAMES = ("WOLTSPACE_PORT", "PORT")
TUNNEL_NAMES = (
    "WOLTSPACE_PUBLIC_TUNNEL", "CLOUDFLARE_TUNNEL_URL", "CLOUDFLARE_TUNNEL_TOKEN",
)


def _pin_scratch_colony(monkeypatch, wolts_dir, port="7799"):
    """Point every name at the scratch colony. Never at a live one."""
    for name in WOLTS_DIR_NAMES:
        monkeypatch.setenv(name, str(wolts_dir))
    for name in PORT_NAMES:
        monkeypatch.setenv(name, str(port))
    monkeypatch.delenv("FORCE_COLOR", raising=False)


def _clear_tunnel_env(monkeypatch):
    """No leaked tunnel configuration may decide what these tests observe."""
    for name in TUNNEL_NAMES:
        monkeypatch.delenv(name, raising=False)


def _layout(tmp_path, isolation="host"):
    return RuntimeLayout(tmp_path / "wolts", ROOT, "127.0.0.1", 7799, isolation)


def _colony(tmp_path, env_lines=()):
    layout = _layout(tmp_path)
    layout.wolts_dir.mkdir(parents=True, exist_ok=True)
    if env_lines:
        (layout.wolts_dir / ".env").write_text("\n".join(env_lines) + "\n")
    return layout


# ---------------------------------------------------------------------------
# Piped output is plain text — the promise every script depends on
# ---------------------------------------------------------------------------


class TestStatusIsFunAndStillFactual:
    """`woltspace status` reads like the lodge. Every fact survives the fun.

    The machine contract is `--json` (see TestStatusJsonIsTheMachineContract);
    the human read gets a creature and a lore line, and then says everything
    it always said, each fact keeping its own `key: value` shape.
    """

    def _scratch(self, tmp_path, monkeypatch):
        _pin_scratch_colony(monkeypatch, tmp_path / "wolts")
        _clear_tunnel_env(monkeypatch)

    def test_a_stopped_lodge_is_the_moon_and_the_facts(
        self, tmp_path, monkeypatch, capsys
    ):
        self._scratch(tmp_path, monkeypatch)
        assert cli_main(["status"]) == 0
        out = capsys.readouterr().out
        assert "\x1b" not in out
        assert out.splitlines() == [
            "  🌙 lodge closed for the night",
            "state: stopped",
            "",
            "endpoint: http://127.0.0.1:7799",
            "  🪵 tunnel: disabled",
            f"  🪵 wolts: {(tmp_path / 'wolts').resolve()}",
        ]

    def test_every_fact_is_still_greppable(self, tmp_path, monkeypatch, capsys):
        """The labels did not move — asserted the way a script reads them.

        `grep 'state:'` matches a substring and would have passed while the
        line sat five columns in with no `endpoint:` label anywhere; the checks
        that actually break are anchored ones. So anchor them.
        """
        self._scratch(tmp_path, monkeypatch)
        cli_main(["status"])
        out = capsys.readouterr().out
        assert re.search(r"^state: stopped$", out, re.M), out
        assert re.search(r"^endpoint: http://127\.0\.0\.1:7799$", out, re.M), out
        assert f"wolts: {(tmp_path / 'wolts').resolve()}" in out

    def test_a_lodge_in_conflict_does_not_get_a_cheerful_line(
        self, tmp_path, monkeypatch, capsys
    ):
        """The state word is coloured by what it means; the emoji matches."""
        self._scratch(tmp_path, monkeypatch)
        monkeypatch.setattr(
            "woltspace.instance.inspect_instance",
            lambda layout: {
                "state": "conflict",
                "endpoint": layout.endpoint,
                "wolts_dir": str(layout.wolts_dir),
            },
        )
        assert cli_main(["status"]) == 1
        out = capsys.readouterr().out
        assert "another lodge holds this ground" in out
        assert "state: conflict" in out
        assert lore.BRICKS in out


class TestStatusJsonIsTheMachineContract:
    """Scripts read `--json`. It carries everything the human read shows."""

    def _running(self, tmp_path, monkeypatch):
        _pin_scratch_colony(monkeypatch, tmp_path / "wolts")
        _clear_tunnel_env(monkeypatch)
        monkeypatch.setattr(
            "woltspace.instance.inspect_instance",
            lambda layout: {
                "state": "healthy",
                "endpoint": layout.endpoint,
                "wolts_dir": str(layout.wolts_dir),
                "owner": {
                    "pid": 4242,
                    "instance_id": "deadbeef",
                    "hostname": "lodge.local",
                },
                "health": {
                    "adoption": {"adopted": ["a"], "orphaned": [], "unchanged": []},
                    "connectors": [
                        {"name": "wolf", "state": "running", "pid": 99,
                         "detail": "cron scheduler", "restarts": 0},
                        {"name": "telegram", "state": "disabled", "pid": None,
                         "detail": "disabled", "remedy": "set a token"},
                    ],
                },
            },
        )

    def test_the_json_verb_exists_and_carries_every_fact(
        self, tmp_path, monkeypatch, capsys
    ):
        self._running(tmp_path, monkeypatch)
        assert cli_main(["status", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["state"] == "healthy"
        assert payload["endpoint"] == "http://127.0.0.1:7799"
        assert payload["wolts_dir"] == str((tmp_path / "wolts").resolve())
        assert payload["owner"]["pid"] == 4242
        assert payload["owner"]["instance_id"] == "deadbeef"
        connectors = {c["name"]: c for c in payload["health"]["connectors"]}
        assert connectors["wolf"]["pid"] == 99
        assert connectors["telegram"]["state"] == "disabled"
        assert payload["health"]["adoption"]["adopted"] == ["a"]

    def test_the_json_is_plain_json_with_no_styling_in_it(
        self, tmp_path, monkeypatch, capsys
    ):
        self._running(tmp_path, monkeypatch)
        cli_main(["status", "--json"])
        out = capsys.readouterr().out
        assert "\x1b" not in out
        for creature in lore.CONNECTOR_CREATURES.values():
            assert creature not in out
        assert lore.TENT not in out

    def test_the_human_read_keeps_every_fact_a_human_reads(
        self, tmp_path, monkeypatch, capsys
    ):
        """Fun, but nothing a person acts on is traded away for it."""
        self._running(tmp_path, monkeypatch)
        assert cli_main(["status"]) == 0
        out = capsys.readouterr().out
        assert "the lodge is open" in out
        assert "owner: lodge.local" in out
        assert "connector wolf: running · cron scheduler" in out
        assert "connector telegram: disabled · disabled" in out
        assert "fix: set a token" in out
        assert "adoption: 1 live · 0 orphaned · 0 unchanged" in out

    def test_the_human_read_drops_the_diagnostics(
        self, tmp_path, monkeypatch, capsys
    ):
        """Pids and the instance hash are for agents, not for eyes.

        Nobody reads a pid off a terminal and does something with it, and a
        32-character hash beside a hostname is noise. Both stay in `--json`.
        """
        self._running(tmp_path, monkeypatch)
        cli_main(["status"])
        out = capsys.readouterr().out
        assert "pid" not in out
        assert "4242" not in out
        assert "deadbeef" not in out
        connector_line = next(
            line for line in out.splitlines() if "connector wolf" in line
        )
        assert connector_line.endswith("cron scheduler")
        # But the machine holding the lodge is still named.
        assert "lodge.local" in out

    def test_the_json_still_carries_every_diagnostic(
        self, tmp_path, monkeypatch, capsys
    ):
        """The agent contract. If this passes, nothing was actually lost."""
        self._running(tmp_path, monkeypatch)
        cli_main(["status", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["owner"]["pid"] == 4242
        assert payload["owner"]["instance_id"] == "deadbeef"
        assert payload["owner"]["hostname"] == "lodge.local"
        connectors = {c["name"]: c for c in payload["health"]["connectors"]}
        assert connectors["wolf"]["pid"] == 99
        assert connectors["telegram"]["pid"] is None

    def test_the_shared_formatter_still_shows_pids_by_default(self):
        """`show_pid=False` is the human render, not a change to the formatter."""
        from woltspace.cli import format_connector_lines

        record = {"health": {"connectors": [
            {"name": "wolf", "state": "running", "detail": "cron", "pid": 99},
        ]}}
        assert format_connector_lines(record) == [
            "connector wolf: running · cron · pid 99",
        ]
        assert format_connector_lines(record, show_pid=False) == [
            "connector wolf: running · cron",
        ]

    def test_each_connector_wears_the_creature_that_does_its_job(
        self, tmp_path, monkeypatch, capsys
    ):
        self._running(tmp_path, monkeypatch)
        cli_main(["status"])
        out = capsys.readouterr().out
        assert "🐺 connector wolf" in out
        assert "🐶 connector telegram" in out
        # A continuation line stays a continuation line — no creature of its own.
        assert "     fix: set a token" in out

    def test_a_wolts_path_with_brackets_is_not_read_as_markup(
        self, tmp_path, monkeypatch, capsys
    ):
        """A path is data. `rich` markup would eat `[old]`; Text objects do not."""
        wolts = tmp_path / "wolts[old]"
        _pin_scratch_colony(monkeypatch, wolts)
        assert cli_main(["status"]) == 0
        assert "wolts[old]" in capsys.readouterr().out


class TestPathsAndDoctorKeepTheirShape:
    def test_paths_prints_one_key_value_line_each(self, tmp_path, monkeypatch, capsys):
        _pin_scratch_colony(monkeypatch, tmp_path / "wolts")
        assert cli_main(["paths"]) == 0
        keys = [line.split(":", 1)[0] for line in capsys.readouterr().out.splitlines()]
        assert keys == [
            "wolts_dir", "state_root", "install_root", "endpoint", "isolation",
        ]

    def test_doctor_keeps_its_glyph_per_check(self, tmp_path, monkeypatch, capsys):
        _pin_scratch_colony(monkeypatch, tmp_path / "wolts")
        cli_main(["doctor", "--no-port"])
        lines = [
            line for line in capsys.readouterr().out.splitlines()
            if line[:1] in {"✓", "!", "✗"}
        ]
        assert lines, "doctor said nothing about any check"
        assert any(line.startswith("✓ python:") for line in lines)


# ---------------------------------------------------------------------------
# The tunnel URL — resolved from configuration, never scraped from a log
# ---------------------------------------------------------------------------


class TestTunnelResolution:
    def test_a_native_lodge_publishes_nothing_by_default(self, tmp_path, monkeypatch):
        _clear_tunnel_env(monkeypatch)
        assert tunnel_settings(_colony(tmp_path))["enabled"] is False

    def test_a_named_tunnel_is_known_from_the_env_file_alone(
        self, tmp_path, monkeypatch
    ):
        """No cloudflared, no state file, no waiting: the URL is config."""
        _clear_tunnel_env(monkeypatch)
        layout = _colony(tmp_path, [
            "WOLTSPACE_PUBLIC_TUNNEL=true",
            "CLOUDFLARE_TUNNEL_TOKEN=a-token",
            "CLOUDFLARE_TUNNEL_URL=https://jerpint.woltspace.com",
        ])
        settings = tunnel_settings(layout)
        assert settings == {
            "enabled": True,
            "kind": "named",
            "url": "https://jerpint.woltspace.com",
        }
        # And the report answers instantly, with no state file in sight.
        assert tunnel_report(layout, wait=False)["url"] == (
            "https://jerpint.woltspace.com"
        )

    def test_half_a_named_tunnel_is_a_quick_tunnel(self, tmp_path, monkeypatch):
        """A URL without a token is not a named tunnel; the URL would be a lie."""
        _clear_tunnel_env(monkeypatch)
        layout = _colony(tmp_path, [
            "WOLTSPACE_PUBLIC_TUNNEL=true",
            "CLOUDFLARE_TUNNEL_URL=https://jerpint.woltspace.com",
        ])
        settings = tunnel_settings(layout)
        assert settings["kind"] == "quick"
        assert settings["url"] == ""

    def test_a_shell_export_beats_the_env_file(self, tmp_path, monkeypatch):
        """Same precedence the supervisor applies when it boots."""
        _clear_tunnel_env(monkeypatch)
        monkeypatch.setenv("WOLTSPACE_PUBLIC_TUNNEL", "false")
        layout = _colony(tmp_path, ["WOLTSPACE_PUBLIC_TUNNEL=true"])
        assert tunnel_settings(layout)["enabled"] is False

    def test_a_quick_tunnel_url_is_read_from_the_state_file(self, tmp_path):
        layout = _colony(tmp_path)
        layout.platform_state.mkdir(parents=True, exist_ok=True)
        (layout.platform_state / "tunnel.json").write_text(
            json.dumps({"url": "https://tiny-forest.trycloudflare.com", "type": "quick"})
        )
        assert read_tunnel_url(layout) == "https://tiny-forest.trycloudflare.com"

    def test_a_dead_tunnel_pid_is_not_a_published_url(self, tmp_path):
        """tunnel.json is only unlinked on a graceful shutdown.

        After a crash it names a cloudflared that is gone, and `status` would
        print its address as live — a public URL that answers nothing. The pid
        is the evidence, and a pid alone is not enough: they get recycled. This
        one belongs to pytest, which is emphatically not a tunnel.
        """
        layout = _colony(tmp_path)
        layout.platform_state.mkdir(parents=True, exist_ok=True)
        (layout.platform_state / "tunnel.json").write_text(json.dumps({
            "url": "https://ghost-forest.trycloudflare.com",
            "pid": os.getpid(),
            "type": "quick",
        }))
        assert read_tunnel_url(layout) == ""

    def test_a_live_cloudflared_pid_is_believed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "woltspace.processes.pid_runs_program",
            lambda pid, program, **kwargs: program == "cloudflared",
        )
        layout = _colony(tmp_path)
        layout.platform_state.mkdir(parents=True, exist_ok=True)
        (layout.platform_state / "tunnel.json").write_text(json.dumps({
            "url": "https://live-forest.trycloudflare.com",
            "pid": 4242,
            "type": "quick",
        }))
        assert read_tunnel_url(layout) == "https://live-forest.trycloudflare.com"

    def test_a_missing_or_broken_state_file_is_silence_not_a_crash(self, tmp_path):
        layout = _colony(tmp_path)
        assert read_tunnel_url(layout) == ""
        layout.platform_state.mkdir(parents=True, exist_ok=True)
        (layout.platform_state / "tunnel.json").write_text("{not json")
        assert read_tunnel_url(layout) == ""

    def test_a_disabled_tunnel_is_never_waited_on(self, tmp_path, monkeypatch):
        """`wait=True` on a disabled tunnel must not sleep for eight seconds."""
        _clear_tunnel_env(monkeypatch)
        monkeypatch.setenv("WOLTSPACE_PUBLIC_TUNNEL", "false")
        monkeypatch.setattr(
            "woltspace.lifecycle.time.sleep",
            lambda _: pytest.fail("waited on a tunnel that is switched off"),
        )
        assert tunnel_report(_colony(tmp_path), wait=True)["url"] == ""


class TestStatusSaysWhetherItIsPublished:
    """A 🌲 line in `status`, read from current state and never waited on."""

    def _scratch(self, tmp_path, monkeypatch, env_lines=(), state=None):
        _clear_tunnel_env(monkeypatch)
        layout = _colony(tmp_path, env_lines)
        _pin_scratch_colony(monkeypatch, tmp_path / "wolts")
        if state is not None:
            layout.platform_state.mkdir(parents=True, exist_ok=True)
            (layout.platform_state / "tunnel.json").write_text(json.dumps(state))
        return layout

    def test_status_never_sleeps_on_a_tunnel(self, tmp_path, monkeypatch, capsys):
        """The property that matters: `status` is a question about right now.

        Publishing is on and no URL has landed — the exact shape that makes
        `start` wait. `status` must not.
        """
        self._scratch(tmp_path, monkeypatch, ["WOLTSPACE_PUBLIC_TUNNEL=true"])
        monkeypatch.setattr(
            "woltspace.lifecycle.time.sleep",
            lambda _: pytest.fail("status waited on a tunnel"),
        )
        assert cli_main(["status"]) == 0
        assert "no public URL yet" in capsys.readouterr().out

    def test_a_live_tunnel_shows_the_public_url(self, tmp_path, monkeypatch, capsys):
        self._scratch(
            tmp_path, monkeypatch, ["WOLTSPACE_PUBLIC_TUNNEL=true"],
            state={"url": "https://tiny-forest.trycloudflare.com", "type": "quick"},
        )
        cli_main(["status"])
        assert (
            f"  {lore.TREE} public: https://tiny-forest.trycloudflare.com"
            in capsys.readouterr().out
        )

    def test_configured_is_not_the_same_as_up(self, tmp_path, monkeypatch, capsys):
        """A named URL from `.env` with no running tunnel is not reachable."""
        self._scratch(tmp_path, monkeypatch, [
            "WOLTSPACE_PUBLIC_TUNNEL=true",
            "CLOUDFLARE_TUNNEL_TOKEN=a-token",
            "CLOUDFLARE_TUNNEL_URL=https://jerpint.woltspace.com",
        ])
        cli_main(["status"])
        out = capsys.readouterr().out
        assert "tunnel configured, not up: https://jerpint.woltspace.com" in out
        assert "public:" not in out

    def test_a_disabled_tunnel_gets_one_quiet_line(self, tmp_path, monkeypatch, capsys):
        """Said, not omitted: whether the lodge is published is worth reading."""
        self._scratch(tmp_path, monkeypatch, ["WOLTSPACE_PUBLIC_TUNNEL=false"])
        cli_main(["status"])
        out = capsys.readouterr().out
        assert "  🪵 tunnel: disabled" in out
        assert lore.TREE not in out

    def test_the_json_carries_the_same_tunnel_shape_as_start(
        self, tmp_path, monkeypatch, capsys
    ):
        self._scratch(
            tmp_path, monkeypatch, ["WOLTSPACE_PUBLIC_TUNNEL=true"],
            state={"url": "https://tiny-forest.trycloudflare.com", "type": "quick"},
        )
        cli_main(["status", "--json"])
        tunnel = json.loads(capsys.readouterr().out)["tunnel"]
        assert sorted(tunnel) == ["enabled", "kind", "live", "url"]
        assert tunnel["enabled"] is True
        assert tunnel["live"] == "https://tiny-forest.trycloudflare.com"
        assert tunnel["url"] == "https://tiny-forest.trycloudflare.com"

    def test_the_report_shape_is_one_shape(self, tmp_path, monkeypatch):
        """`start` and `status` read the same resolver, so the keys must match."""
        layout = self._scratch(tmp_path, monkeypatch, [
            "WOLTSPACE_PUBLIC_TUNNEL=true",
            "CLOUDFLARE_TUNNEL_TOKEN=a-token",
            "CLOUDFLARE_TUNNEL_URL=https://jerpint.woltspace.com",
        ])
        assert sorted(tunnel_report(layout, wait=False)) == sorted(
            tunnel_report(layout, wait=True)
        )


class TestTunnelLines:
    def test_a_disabled_tunnel_says_so_in_one_line(self, capsys):
        lore.public_tunnel_lines({"enabled": False, "kind": "quick", "url": ""})
        said = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
        assert len(said) == 1
        assert "tunnel disabled" in said[0]

    def test_a_known_url_comes_with_the_warning(self, capsys):
        lore.public_tunnel_lines({
            "enabled": True, "kind": "named", "url": "https://jerpint.woltspace.com",
        })
        out = capsys.readouterr().out
        assert "public: https://jerpint.woltspace.com" in out
        assert lore.SHARE_WARNING in out
        assert "opening a path to the outside" in out

    def test_a_tunnel_with_no_url_yet_admits_it(self, capsys):
        lore.public_tunnel_lines({"enabled": True, "kind": "quick", "url": ""})
        out = capsys.readouterr().out
        assert "starting tunnel" in out
        assert "still digging" in out
        assert "public:" not in out


# ---------------------------------------------------------------------------
# House style — the copy rules the colony holds itself to
# ---------------------------------------------------------------------------


def _every_phrase() -> list[str]:
    phrases = [lore.SHARE_WARNING, lore.TUNNEL_DIGGING]
    for _, headline, subtitle in lore.TRANSITIONS.values():
        phrases.extend([headline, subtitle])
    return phrases


class TestTheVoice:
    def test_taglines_use_a_single_dash(self):
        """No em dashes in anything the CLI says out loud."""
        for phrase in _every_phrase():
            assert "—" not in phrase, phrase

    def test_nothing_names_the_machinery(self):
        """User-facing copy is wolts and lore — never the model behind them."""
        for phrase in _every_phrase():
            lowered = phrase.lower()
            assert "claude" not in lowered, phrase
            assert " ai " not in f" {lowered} ", phrase

    def test_the_share_warning_is_the_launcher_s_own_words(self):
        assert lore.SHARE_WARNING == (
            "share carefully - anyone with this link can reach your wolts."
        )

    def test_every_lifecycle_state_has_a_creature_and_a_lore_line(self):
        for key in ("started", "running", "waking", "stopped", "quiet", "broken"):
            emoji, headline, subtitle = lore.TRANSITIONS[key]
            assert emoji and headline and subtitle
            assert "lodge" in f"{headline} {subtitle}"

    def test_the_dog_emoji_is_the_dog_face(self):
        """🐶, never 🐕 — and no dog in the lifecycle set at all."""
        source = (ROOT / "src" / "woltspace" / "lore.py").read_text()
        assert "🐕" not in source

    def test_a_missing_rich_still_prints_the_words(self, capsys):
        """Colour is a courtesy. The lines are not optional."""
        console = lore._PlainConsole()
        console.print("  🌙 the lodge stopped")
        assert capsys.readouterr().out == "  🌙 the lodge stopped\n"


class TestTheseTestsCannotBeShadowed:
    """Suite-order immunity, pinned rather than hoped for.

    These exact-output tests passed alone and failed when run after tests that
    write the *canonical* `WOLTSPACE_*` names into the real `os.environ`: a
    leaked `WOLTSPACE_WOLTS_DIR` shadowed a test that had pinned only the
    legacy `WOLTS_DIR`, and the run resolved against something that was not
    the scratch colony. The fix is to pin both spellings; this is the test that
    says so, so nobody quietly drops one half later.
    """

    def test_the_pin_sets_every_spelling(self, tmp_path, monkeypatch):
        import os

        _pin_scratch_colony(monkeypatch, tmp_path / "wolts", port="7799")
        for name in WOLTS_DIR_NAMES:
            assert os.environ[name] == str(tmp_path / "wolts"), name
        for name in PORT_NAMES:
            assert os.environ[name] == "7799", name

    def test_a_leaked_canonical_name_cannot_redirect_a_test(
        self, tmp_path, monkeypatch, capsys
    ):
        """The leak, reproduced: canonical set to a bogus path, then pinned."""
        monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", "/nowhere/that/exists")
        monkeypatch.setenv("WOLTSPACE_PORT", "1234")
        _pin_scratch_colony(monkeypatch, tmp_path / "wolts")
        _clear_tunnel_env(monkeypatch)
        assert cli_main(["status"]) == 0
        out = capsys.readouterr().out
        assert "/nowhere/that/exists" not in out
        assert "1234" not in out
        assert str((tmp_path / "wolts").resolve()) in out
        assert "http://127.0.0.1:7799" in out

    def test_a_leaked_legacy_name_cannot_redirect_a_test_either(
        self, tmp_path, monkeypatch, capsys
    ):
        """Immunity runs both directions — whichever spelling wins resolution."""
        monkeypatch.setenv("WOLTS_DIR", "/nowhere/that/exists")
        _pin_scratch_colony(monkeypatch, tmp_path / "wolts")
        _clear_tunnel_env(monkeypatch)
        assert cli_main(["status"]) == 0
        out = capsys.readouterr().out
        assert "/nowhere/that/exists" not in out
        assert str((tmp_path / "wolts").resolve()) in out


class TestTheVoiceFallback:
    def test_the_plain_console_prints_the_words(self, capsys):
        """Colour is a courtesy. The lines are not optional."""
        console = lore._PlainConsole()
        console.print("  🌙 the lodge stopped")
        assert capsys.readouterr().out == "  🌙 the lodge stopped\n"
