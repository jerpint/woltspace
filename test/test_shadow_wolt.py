"""The shadow wolt: the only wolt a live-server test is allowed to touch.

Borrowing a real wolt means spawning agents into someone's actual directory
and leaving debris there. These tests guard the fixture's contract, because
the contract is the whole point of it.
"""

import json
import re
from pathlib import Path

import pytest

from conftest import SHADOW_MARKER, SHADOW_WOLT, shadow_is_reusable

TEST_DIR = Path(__file__).resolve().parent


class TestFixtureLifecycle:
    def test_it_exists_on_disk_and_is_discoverable_as_a_wolt(self, shadow_wolt):
        import os

        home = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts")) / shadow_wolt
        assert home.is_dir()
        manifest = json.loads((home / "wolt" / "wolt.json").read_text())
        assert manifest["name"] == SHADOW_WOLT
        assert manifest["test_fixture"] is True
        assert (home / SHADOW_MARKER).is_file()

    def test_it_is_gone_afterwards(self):
        # This test runs without the fixture: nothing should be left over.
        import os

        wolts_dir = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))
        assert not (wolts_dir / SHADOW_WOLT).exists()

    def test_its_name_is_recognizably_a_test_artifact(self):
        assert "test" in SHADOW_WOLT


class TestDeletionGuard:
    def test_a_directory_the_suite_created_may_be_removed(self, tmp_path):
        home = tmp_path / SHADOW_WOLT
        home.mkdir()
        (home / SHADOW_MARKER).write_text("created by tests")
        assert shadow_is_reusable(home) is True

    def test_a_real_directory_of_that_name_is_never_touched(self, tmp_path):
        home = tmp_path / SHADOW_WOLT
        (home / "wolt" / "memory").mkdir(parents=True)
        (home / "wolt" / "memory" / "identity.md").write_text("someone's real wolt")
        assert shadow_is_reusable(home) is False

    def test_an_absent_directory_is_fine(self, tmp_path):
        assert shadow_is_reusable(tmp_path / SHADOW_WOLT) is True


class TestNoRealWoltIsTargeted:
    """Structural guarantee: no test names a real wolt as a spawn target."""

    SPAWN_TARGET = re.compile(r'"wolt"\s*:\s*"([a-zA-Z][\w-]*)"')
    ALLOWED = {SHADOW_WOLT, "nonexistent-wolt-xyz"}

    def test_no_live_spawn_hardcodes_a_wolt_name(self):
        offenders = []
        for path in sorted(TEST_DIR.glob("test_*.py")):
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if "sessions/new" not in line and "server_post" not in line:
                    continue
                for name in self.SPAWN_TARGET.findall(line):
                    if name not in self.ALLOWED:
                        offenders.append(f"{path.name}:{number} → {name}")
        assert offenders == [], (
            "live-server spawns must target the shadow wolt fixture: "
            + ", ".join(offenders)
        )

    def test_the_conftest_exposes_no_way_to_name_a_real_wolt(self):
        source = (TEST_DIR / "conftest.py").read_text()
        assert "any_wolt" not in source, (
            "dynamic real-wolt selection was replaced by the shadow fixture"
        )
        assert "neowolt" not in source


class TestNoRealUserStateIsDiscovered:
    """Tests must be told where to send, never find out on their own."""

    def test_the_chat_id_helper_does_not_read_the_registry(self):
        """A discovered chat_id is a real person's phone."""
        import inspect

        from test_closed_loop import _find_chat_id

        body = inspect.getsource(_find_chat_id)
        for forbidden in ("REGISTRY_DIR", "glob(", "json.loads"):
            assert forbidden not in body, (
                f"_find_chat_id must not scrape the registry (found {forbidden!r})"
            )

    def test_the_chat_id_helper_reads_only_the_configured_env(self):
        from test_closed_loop import _find_chat_id

        import os

        previous = os.environ.pop("TEST_CHAT_ID", None)
        try:
            assert _find_chat_id() is None, (
                "without TEST_CHAT_ID the tests must skip, not find a real chat"
            )
        finally:
            if previous is not None:
                os.environ["TEST_CHAT_ID"] = previous

    def test_live_sends_are_opt_in(self):
        import os

        from conftest import _live_send_enabled

        previous = os.environ.pop("WOLTSPACE_TEST_LIVE_SEND", None)
        try:
            assert _live_send_enabled() is False
        finally:
            if previous is not None:
                os.environ["WOLTSPACE_TEST_LIVE_SEND"] = previous

    def test_every_live_send_is_gated(self):
        """Each real send site carries the opt-in marker."""
        gated = {
            "test_closed_loop.py": [
                "test_bot_can_send_to_chat", "TestNotifySeam",
                "test_notify_with_freshly_created_session",
            ],
            "test_server_health.py": ["test_notify_returns_adapter"],
            "test_telegram_loop.py": ["test_server_notify_json_contract"],
        }
        for filename, names in gated.items():
            lines = (TEST_DIR / filename).read_text().splitlines()
            for name in names:
                index = next(
                    i for i, line in enumerate(lines)
                    if line.strip().startswith((f"def {name}", f"class {name}"))
                )
                preceding = "\n".join(lines[max(index - 4, 0):index])
                assert "requires_live_send" in preceding, f"{filename}::{name} is ungated"


class TestRoutingIsActuallyResolvable:
    """The bug that sent test messages to a real person's phone.

    `/notify` resolves a session's routing by scanning
    `wolts/{wolt}/.state/sessions/`. The closed-loop tests built their registry
    with `SessionRegistry(WOLTS_DIR/".state"/"registry")` — SessionRegistry
    takes the *wolts* dir, so records landed somewhere that scan never looks.
    Routing came back empty, and notify fell through to its last resort: the
    first entry in TELEGRAM_ALLOWED_USERS. TEST_CHAT_ID was set correctly the
    whole time; it just never reached the send.
    """

    def test_a_routed_session_is_found_where_notify_looks(self, shadow_wolt):
        import os
        import sys

        sys.path.insert(0, str(TEST_DIR.parent / "container" / "lib"))
        from sessions import SessionRegistry

        from test_closed_loop import REGISTRY_ROOT

        wolts_dir = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))
        assert REGISTRY_ROOT == wolts_dir, (
            "SessionRegistry takes the wolts dir; anything else hides routing "
            "from the server and notify falls back to a real user"
        )

        name = "test-shadow-routing-probe"
        registry = SessionRegistry(REGISTRY_ROOT)
        registry.create(
            name, wolt=shadow_wolt, creature="beaver",
            adapter="telegram", chat_id="-1000000000",
        )
        try:
            record = wolts_dir / shadow_wolt / ".state" / "sessions" / f"{name}.json"
            assert record.is_file(), (
                f"notify scans wolts/*/.state/sessions/; nothing at {record}"
            )
            assert json.loads(record.read_text())["chat_id"] == "-1000000000"
        finally:
            registry.delete(name, wolt=shadow_wolt)

    def test_notify_falls_back_to_a_real_user_when_routing_is_missing(self):
        """Why an unfindable record is dangerous, not merely wrong."""
        notify = (TEST_DIR.parent / "server" / "notify.py").read_text()
        assert "TELEGRAM_ALLOWED_USERS" in notify
        assert "read_session_registry" in notify


class TestOptInTierIsRunnable:
    """Gated must mean "run before release", not "never run"."""

    RUNNER = TEST_DIR / "run-tests.sh"

    def test_the_runner_has_a_one_command_opt_in_tier(self):
        text = self.RUNNER.read_text()
        assert "  opt-in)" in text
        assert "WOLTSPACE_TEST_REAL_SPAWN=1" in text
        assert "WOLTSPACE_TEST_LIVE_SEND=1" in text

    def test_it_refuses_to_run_without_a_named_test_chat(self):
        text = self.RUNNER.read_text()
        tier = text.split("  opt-in)")[1].split(";;")[0]
        assert 'if [ -z "${TEST_CHAT_ID:-}" ]' in tier
        assert "exit 1" in tier

    def test_it_covers_the_seams_it_claims_to(self):
        tier = self.RUNNER.read_text().split("  opt-in)")[1].split(";;")[0]
        for path in ("test/test_server_health.py", "test/test_closed_loop.py",
                     "test/test_telegram_loop.py"):
            assert path in tier, path

    def test_the_release_checklist_runs_it(self):
        doc = (TEST_DIR.parent / "docs" / "native-and-container.md").read_text()
        assert "run-tests.sh opt-in" in doc
        assert doc.index("run-tests.sh opt-in") < doc.index("Publish PyPI and npm together")


class TestFirstRunDocIsHonest:
    """The doc is a walkthrough someone follows on a machine we cannot test."""

    DOC = TEST_DIR.parent / "docs" / "native-first-run.md"

    def test_it_flags_what_could_not_be_verified_on_macos(self):
        text = self.DOC.read_text()
        assert "(untested on macOS)" in text
        lines = text.splitlines()
        commands = [i for i, line in enumerate(lines) if line.strip().startswith("brew ")]
        assert commands, "the prerequisites section should show the brew commands"
        # Every brew command sits in a section that admits it is unverified.
        for index in commands:
            context = "\n".join(lines[max(index - 6, 0):index])
            assert "untested on macOS" in context, lines[index]

    def test_the_commands_it_teaches_are_the_ones_the_cli_has(self):
        import sys

        sys.path.insert(0, str(TEST_DIR.parent / "src"))
        from woltspace.cli import build_parser

        text = self.DOC.read_text()
        subcommands = set(build_parser()._subparsers._group_actions[0].choices)
        for name in ("paths", "doctor", "start", "status", "stop", "tui"):
            assert name in subcommands
            assert f"woltspace {name}" in text, name

    def test_the_pre_publish_tarball_name_matches_the_pinned_version(self):
        from woltspace.compatibility import TUI_VERSION

        assert f"woltspace-tui-{TUI_VERSION}.tgz" in self.DOC.read_text()

    def test_it_is_linked_from_the_readme(self):
        assert "docs/native-first-run.md" in (TEST_DIR.parent / "README.md").read_text()
