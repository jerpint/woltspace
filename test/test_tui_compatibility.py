"""Python and npm artifacts declare one exact compatibility pair."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from packaging.version import Version

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from woltspace import __version__
from woltspace.compatibility import TUI_BINARY, TUI_PACKAGE, TUI_VERSION, tui_spec
from woltspace.tui import TuiResolutionError, resolve_tui


def test_python_embeds_exact_scoped_tui_version():
    manifest = json.loads(
        (Path(__file__).resolve().parent.parent / "tui" / "package.json").read_text()
    )
    assert manifest["name"] == TUI_PACKAGE == "@woltspace/tui"
    # The npm pin must match the npm manifest byte-for-byte — it is what npx
    # resolves. The Python version is the same release in PEP 440 spelling,
    # which writes pre-releases without semver's separators ("0.5.0rc2" vs
    # "0.5.0-rc.2"), so those two compare normalized.
    assert manifest["version"] == TUI_VERSION
    assert Version(TUI_VERSION) == Version(__version__)
    assert TUI_BINARY == "woltspace-tui"
    assert tui_spec() == "@woltspace/tui@0.5.0-rc.2"


def _runner(payload, returncode=0):
    return lambda *args, **kwargs: SimpleNamespace(
        stdout=json.dumps(payload), stderr="", returncode=returncode,
    )


def _fallback_runner(payload):
    def run(command, **kwargs):
        if command[-1] == "--version" and command[0].endswith("node"):
            return SimpleNamespace(stdout="v22.0.0\n", stderr="", returncode=0)
        return SimpleNamespace(
            stdout=json.dumps(payload), stderr="", returncode=0,
        )
    return run


def test_exact_local_binary_is_preferred():
    resolution = resolve_tui(
        {},
        which=lambda name: "/tools/woltspace-tui" if name == TUI_BINARY else "/tools/npx",
        runner=_runner({
            "name": TUI_PACKAGE, "version": TUI_VERSION, "binary": TUI_BINARY,
        }),
    )
    assert resolution.source == "local"
    assert resolution.command == ("/tools/woltspace-tui",)


def test_mismatched_local_binary_falls_back_to_exact_npx_spec():
    resolution = resolve_tui(
        {},
        which=lambda name: f"/tools/{name}",
        runner=_fallback_runner({
            "name": TUI_PACKAGE, "version": "0.2.1", "binary": TUI_BINARY,
        }),
    )
    assert resolution.source == "npx"
    assert resolution.command == (
        "/tools/npx", "--yes", "--package=@woltspace/tui@0.5.0-rc.2", "woltspace-tui",
    )
    assert resolution.local_probe["version"] == "0.2.1"


def test_missing_exact_binary_and_npx_has_actionable_remedy():
    with pytest.raises(TuiResolutionError, match="Install Node.js 18 or newer"):
        resolve_tui({}, which=lambda name: None)


def test_old_node_has_exact_remedy_before_npx_launch():
    def which(name):
        return f"/tools/{name}" if name in {"node", "npx"} else None

    with pytest.raises(TuiResolutionError, match="Found Node.js 16"):
        resolve_tui(
            {}, which=which,
            runner=lambda *a, **kw: SimpleNamespace(
                stdout="v16.20.0\n", stderr="", returncode=0,
            ),
        )
