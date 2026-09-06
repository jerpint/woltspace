"""The shadow wolt: the only wolt a live-server test is allowed to touch.

Borrowing a real wolt means spawning agents into someone's actual directory
and leaving debris there. These tests guard the fixture's contract, because
the contract is the whole point of it.
"""

import json
import re
from pathlib import Path

import pytest

from conftest import SHADOW_MARKER, SHADOW_PREFIX, SHADOW_WOLT, shadow_is_reusable

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
    def test_a_directory_this_run_created_may_be_removed(self, tmp_path):
        import os

        home = tmp_path / SHADOW_WOLT
        home.mkdir()
        (home / SHADOW_MARKER).write_text(f"created by tests pid={os.getpid()}\n")
        assert shadow_is_reusable(home) is True

    def test_a_live_concurrent_runs_directory_is_left_alone(self, tmp_path):
        """Two suite runs share WOLTS_DIR; neither may rmtree the other's."""
        import os

        home = tmp_path / SHADOW_WOLT
        home.mkdir()
        (home / SHADOW_MARKER).write_text(f"created by tests pid={os.getppid()}\n")
        assert shadow_is_reusable(home) is False

    def test_a_finished_runs_leftovers_may_be_cleared(self, tmp_path):
        home = tmp_path / SHADOW_WOLT
        home.mkdir()
        (home / SHADOW_MARKER).write_text("created by tests pid=999999\n")
        assert shadow_is_reusable(home) is True

    def test_each_run_gets_its_own_name(self):
        import os

        assert SHADOW_WOLT == f"{SHADOW_PREFIX}-{os.getpid()}"

    def test_a_real_directory_of_that_name_is_never_touched(self, tmp_path):
        home = tmp_path / SHADOW_WOLT
        (home / "wolt" / "memory").mkdir(parents=True)
        (home / "wolt" / "memory" / "identity.md").write_text("someone's real wolt")
        assert shadow_is_reusable(home) is False

    def test_an_absent_directory_is_fine(self, tmp_path):
        assert shadow_is_reusable(tmp_path / SHADOW_WOLT) is True


class TestNoRealWoltIsTargeted:
    """Structural guarantee: no test names a real wolt as a spawn target.

    Parsed, not grepped. A line-by-line regex misses the shapes that actually
    occur — a multi-line dict literal, a kwarg, an f-string endpoint — which
    means it passes for the wrong reason.
    """

    ALLOWED = {"nonexistent-wolt-xyz"}

    def _is_allowed(self, name: str) -> bool:
        return name in self.ALLOWED or name.startswith(SHADOW_PREFIX)

    def _live_registry_files(self):
        """Files that build a registry on the real data root, not tmp_path."""
        return sorted(TEST_DIR.glob("test_*.py"))

    def test_no_live_spawn_hardcodes_a_wolt_name(self):
        import ast

        offenders = []
        for path in self._live_registry_files():
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if not self._targets_the_server(node):
                    continue
                for value in self._wolt_literals(node):
                    if not self._is_allowed(value):
                        offenders.append(f"{path.name}:{node.lineno} → {value}")
        assert offenders == [], (
            "live-server spawns must target the shadow wolt fixture: "
            + ", ".join(offenders)
        )

    @staticmethod
    def _targets_the_server(node) -> bool:
        import ast

        callee = node.func
        name = getattr(callee, "id", None) or getattr(callee, "attr", None) or ""
        if name in {"server_post", "_server_post"}:
            return True
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                if "sessions/new" in arg.value:
                    return True
            if isinstance(arg, ast.JoinedStr):
                for part in arg.values:
                    if isinstance(part, ast.Constant) and "sessions/new" in str(part.value):
                        return True
        return False

    @staticmethod
    def _wolt_literals(node):
        """Every literal `wolt` value in the call — dict entry or keyword."""
        import ast

        found = []
        for keyword in node.keywords:
            if keyword.arg == "wolt" and isinstance(keyword.value, ast.Constant):
                if isinstance(keyword.value.value, str):
                    found.append(keyword.value.value)
        for arg in node.args:
            if not isinstance(arg, ast.Dict):
                continue
            for key, value in zip(arg.keys, arg.values):
                if (
                    isinstance(key, ast.Constant) and key.value == "wolt"
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                ):
                    found.append(value.value)
        return found

    def test_the_parser_catches_the_shapes_a_regex_missed(self, tmp_path):
        """Guard the guard: a multi-line literal and a kwarg must both be seen."""
        import ast

        source = tmp_path / "test_probe.py"
        source.write_text(
            "def t(server_post):\n"
            "    server_post(\n"
            '        "/sessions/new/lodge",\n'
            '        {"wolt": "somebodys-real-wolt"},\n'
            "    )\n"
        )
        tree = ast.parse(source.read_text())
        names = [
            value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and self._targets_the_server(node)
            for value in self._wolt_literals(node)
        ]
        assert names == ["somebodys-real-wolt"]
        assert not self._is_allowed("somebodys-real-wolt")

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

    # A live marker is either a direct call to Telegram / the notify endpoint,
    # or a call to one of the helpers that makes one.
    LIVE_CALLS = (
        "api.telegram.org", '"/notify"',
        "_telegram_send(", "_telegram_get_updates(", "_tg_send_transcript(",
    )
    # Fixtures that already skip unless the live gate and TEST_CHAT_ID are set.
    GATING_FIXTURES = {"routed_test_session", "test_chat_id", "spawned_lodge_session"}

    def test_every_live_telegram_call_sits_under_a_gate(self):
        """Found by parsing, not by a hand-kept list.

        The previous version enumerated the gated sites from a dict, so it
        could only confirm what someone had already remembered — which is why
        an ungated live `getUpdates` survived the first sweep.
        """
        import ast

        offenders = []
        for path in sorted(TEST_DIR.glob("test_*.py")):
            text = path.read_text()
            if not any(marker in text for marker in self.LIVE_CALLS):
                continue
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                body = ast.get_source_segment(text, node) or ""
                if not any(marker in body for marker in self.LIVE_CALLS):
                    continue
                if node.name.startswith("_"):
                    continue  # a helper; its callers are what must be gated
                if (
                    self._gated(node)
                    or self._gated_by_owner(tree, node)
                    or self._gated_by_fixture(node)
                    or self._skips(body)
                    or self._in_process(text, node, body)
                ):
                    continue
                offenders.append(f"{path.name}::{node.name}")
        assert offenders == [], (
            "these touch the live bot without an opt-in gate: " + ", ".join(offenders)
        )

    @staticmethod
    def _gated(node) -> bool:
        import ast

        names = []
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            names.append(getattr(target, "id", None) or getattr(target, "attr", "") or "")
        return any("live" in name for name in names)

    def _gated_by_owner(self, tree, node) -> bool:
        """A gate on the enclosing class covers its methods."""
        import ast

        for parent in ast.walk(tree):
            if isinstance(parent, ast.ClassDef) and node in parent.body:
                return self._gated(parent)
        return False

    def _gated_by_fixture(self, node) -> bool:
        """Requesting a fixture that skips without the gate counts as gated."""
        names = {arg.arg for arg in node.args.args}
        return bool(names & self.GATING_FIXTURES)

    @staticmethod
    def _skips(body: str) -> bool:
        """A test that resolves the chat itself and skips without one is safe."""
        return "pytest.skip" in body and "_find_chat_id" in body

    @staticmethod
    def _in_process(text: str, node, body: str) -> bool:
        """A starlette TestClient calls the ASGI app directly — no socket.

        `POST "/notify"` against one cannot reach a bot, a chat, or anything
        outside the process, so it needs no live gate. Deliberately narrow:
        the module must import TestClient and the test must actually use one,
        and only the notify *endpoint* marker is excused — a literal
        `api.telegram.org` in the same test would still be an offence.
        """
        if "from starlette.testclient import TestClient" not in text:
            return False
        if "api.telegram.org" in body:
            return False
        return "TestClient(" in body or "self._client(" in body


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

        from test_closed_loop import WOLTS_DIR as CLOSED_LOOP_WOLTS_DIR

        assert REGISTRY_ROOT == CLOSED_LOOP_WOLTS_DIR, (
            "SessionRegistry takes the wolts dir; anything else hides routing "
            "from the server and notify falls back to a real user"
        )
        wolts_dir = REGISTRY_ROOT

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

    def test_the_quoted_connector_remedy_matches_the_one_the_code_emits(self):
        """The doc quotes this string verbatim; drift is how it went stale."""
        import sys

        sys.path.insert(0, str(TEST_DIR.parent / "src"))
        from woltspace.channels import TelegramConnector
        from woltspace.layout import RuntimeLayout

        layout = RuntimeLayout(
            wolts_dir=TEST_DIR.parent / "nonexistent-data-root",
            install_root=TEST_DIR.parent,
        )
        import woltspace.channels as channels

        # Force the missing-extra branch: it is what a fresh native install
        # hits, and the suite's own venv always has the dependency.
        original = channels._module_available
        channels._module_available = lambda name: name != "telegram"
        try:
            plan = TelegramConnector().plan(layout, {
                "WOLTSPACE_ENTRYPOINT": "1",
                "ENABLE_TELEGRAM_BOT": "true",
                "TELEGRAM_BOT_TOKEN": "1:t",
                "TELEGRAM_BOT_DIR": "/nowhere",
            })
        finally:
            channels._module_available = original

        assert "python-telegram-bot is not installed" in plan.detail
        assert "'.[connectors]'" in plan.remedy, (
            "pre-publish, the PyPI form is a dead end — name the checkout first"
        )
        assert plan.remedy in self.DOC.read_text(), (
            "the doc quotes this remedy verbatim; they have drifted apart"
        )

    def test_the_pre_publish_tarball_name_matches_the_pinned_version(self):
        from woltspace.compatibility import TUI_VERSION

        assert f"woltspace-tui-{TUI_VERSION}.tgz" in self.DOC.read_text()

    def test_it_is_linked_from_the_readme(self):
        assert "docs/native-first-run.md" in (TEST_DIR.parent / "README.md").read_text()
