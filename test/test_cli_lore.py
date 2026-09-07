"""The lodge's voice, and the promises that keep it safe to speak.

Two things are under test here, and they pull in opposite directions:

* the CLI says the lore lines the launcher said, in the colours it used;
* nothing it says breaks a script, so a piped `woltspace status` is the exact
  plain text it always was.

Usage: uv run pytest test/test_cli_lore.py -v
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from woltspace import lore  # noqa: E402
from woltspace.cli import main as cli_main  # noqa: E402
from woltspace.layout import RuntimeLayout  # noqa: E402
from woltspace.lifecycle import read_tunnel_url, tunnel_report, tunnel_settings  # noqa: E402


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
        monkeypatch.setenv("WOLTS_DIR", str(tmp_path / "wolts"))
        monkeypatch.setenv("WOLTSPACE_PORT", "7799")
        monkeypatch.delenv("FORCE_COLOR", raising=False)

    def test_a_stopped_lodge_is_the_moon_and_the_facts(
        self, tmp_path, monkeypatch, capsys
    ):
        self._scratch(tmp_path, monkeypatch)
        assert cli_main(["status"]) == 0
        out = capsys.readouterr().out
        assert "\x1b" not in out
        assert out.splitlines() == [
            "  🌙 lodge closed for the night",
            "     state: stopped",
            "",
            "  🦫 http://127.0.0.1:7799",
            f"  🪵 wolts: {(tmp_path / 'wolts').resolve()}",
        ]

    def test_every_fact_is_still_greppable(self, tmp_path, monkeypatch, capsys):
        """`grep 'state:'`, `grep 'wolts:'` — the labels did not move."""
        self._scratch(tmp_path, monkeypatch)
        cli_main(["status"])
        out = capsys.readouterr().out
        assert "state: stopped" in out
        assert "http://127.0.0.1:7799" in out
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
        monkeypatch.setenv("WOLTS_DIR", str(tmp_path / "wolts"))
        monkeypatch.setenv("WOLTSPACE_PORT", "7799")
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

    def test_the_human_read_keeps_the_owner_pid_and_connector_pids(
        self, tmp_path, monkeypatch, capsys
    ):
        """Fun, but no fact is traded away for it."""
        self._running(tmp_path, monkeypatch)
        assert cli_main(["status"]) == 0
        out = capsys.readouterr().out
        assert "the lodge is open" in out
        assert "owner: pid 4242 · deadbeef · lodge.local" in out
        assert "connector wolf: running · cron scheduler · pid 99" in out
        assert "connector telegram: disabled · disabled" in out
        assert "fix: set a token" in out
        assert "adoption: 1 live · 0 orphaned · 0 unchanged" in out

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
        monkeypatch.setenv("WOLTS_DIR", str(wolts))
        monkeypatch.setenv("WOLTSPACE_PORT", "7799")
        assert cli_main(["status"]) == 0
        assert "wolts[old]" in capsys.readouterr().out


class TestPathsAndDoctorKeepTheirShape:
    def test_paths_prints_one_key_value_line_each(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("WOLTS_DIR", str(tmp_path / "wolts"))
        assert cli_main(["paths"]) == 0
        keys = [line.split(":", 1)[0] for line in capsys.readouterr().out.splitlines()]
        assert keys == [
            "wolts_dir", "state_root", "install_root", "endpoint", "isolation",
        ]

    def test_doctor_keeps_its_glyph_per_check(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("WOLTS_DIR", str(tmp_path / "wolts"))
        monkeypatch.setenv("WOLTSPACE_PORT", "7799")
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
        monkeypatch.delenv("WOLTSPACE_PUBLIC_TUNNEL", raising=False)
        monkeypatch.delenv("CLOUDFLARE_TUNNEL_URL", raising=False)
        monkeypatch.delenv("CLOUDFLARE_TUNNEL_TOKEN", raising=False)
        assert tunnel_settings(_colony(tmp_path))["enabled"] is False

    def test_a_named_tunnel_is_known_from_the_env_file_alone(
        self, tmp_path, monkeypatch
    ):
        """No cloudflared, no state file, no waiting: the URL is config."""
        for key in ("WOLTSPACE_PUBLIC_TUNNEL", "CLOUDFLARE_TUNNEL_URL",
                    "CLOUDFLARE_TUNNEL_TOKEN"):
            monkeypatch.delenv(key, raising=False)
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
        for key in ("WOLTSPACE_PUBLIC_TUNNEL", "CLOUDFLARE_TUNNEL_URL",
                    "CLOUDFLARE_TUNNEL_TOKEN"):
            monkeypatch.delenv(key, raising=False)
        layout = _colony(tmp_path, [
            "WOLTSPACE_PUBLIC_TUNNEL=true",
            "CLOUDFLARE_TUNNEL_URL=https://jerpint.woltspace.com",
        ])
        settings = tunnel_settings(layout)
        assert settings["kind"] == "quick"
        assert settings["url"] == ""

    def test_a_shell_export_beats_the_env_file(self, tmp_path, monkeypatch):
        """Same precedence the supervisor applies when it boots."""
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

    def test_a_missing_or_broken_state_file_is_silence_not_a_crash(self, tmp_path):
        layout = _colony(tmp_path)
        assert read_tunnel_url(layout) == ""
        layout.platform_state.mkdir(parents=True, exist_ok=True)
        (layout.platform_state / "tunnel.json").write_text("{not json")
        assert read_tunnel_url(layout) == ""

    def test_a_disabled_tunnel_is_never_waited_on(self, tmp_path, monkeypatch):
        """`wait=True` on a disabled tunnel must not sleep for eight seconds."""
        monkeypatch.setenv("WOLTSPACE_PUBLIC_TUNNEL", "false")
        monkeypatch.setattr(
            "woltspace.lifecycle.time.sleep",
            lambda _: pytest.fail("waited on a tunnel that is switched off"),
        )
        assert tunnel_report(_colony(tmp_path), wait=True)["url"] == ""


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
