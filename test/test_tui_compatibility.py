"""The wheel and the npm package are versioned independently.

One direction only: the tui declares the minimum `woltspace` it needs and
checks it itself at startup. The wheel never checks the tui's version — its
probe asks a candidate binary to prove it is the right bin of the right
package, and nothing more. These tests hold that line, because the easiest
regression here is quietly reintroducing a pin.
"""

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from packaging.version import Version

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from woltspace import __version__
from woltspace import compatibility
from woltspace.compatibility import TUI_BINARY, TUI_PACKAGE, TUI_SERVICE_BINARY, tui_spec
from woltspace.tui import TuiResolutionError, _probe, local_tarball_recipe, resolve_tui


def _manifest():
    return json.loads((ROOT / "tui" / "package.json").read_text())


def test_the_python_side_pins_no_tui_version():
    """No constant to keep in lockstep — that is the whole point of the change."""
    assert not hasattr(compatibility, "TUI_VERSION")
    source = (ROOT / "src" / "woltspace").rglob("*.py")
    for path in source:
        assert "TUI_VERSION" not in path.read_text(), path


def test_the_npx_fallback_asks_for_latest():
    assert TUI_PACKAGE == "@woltspace/tui" == _manifest()["name"]
    assert TUI_BINARY == "woltspace-tui"
    assert tui_spec() == "@woltspace/tui@latest"


def test_the_tui_names_both_bins_the_python_side_looks_for():
    bins = _manifest()["bin"]
    assert set(bins) == {TUI_BINARY, TUI_SERVICE_BINARY}


def test_the_tui_never_requires_an_unreleased_lodge():
    """A tui that needs a woltspace nobody can install is unshippable."""
    source = (ROOT / "tui" / "src" / "version.js").read_text()
    declared = re.search(r"minLodgeVersion\s*=\s*'([^']+)'", source)
    assert declared, "tui/src/version.js must declare minLodgeVersion"
    assert Version(declared.group(1)) <= Version(__version__)


def test_the_from_checkout_recipe_names_no_patch_version():
    """`npm pack` writes whatever version is in the manifest; glob for it."""
    recipe = local_tarball_recipe()
    assert "npm pack" in recipe
    assert "./woltspace-tui-*.tgz" in recipe
    assert not re.search(r"woltspace-tui-\d", recipe)


# ---------------------------------------------------------------------------
# The probe: identity, not version
# ---------------------------------------------------------------------------

def _runner(payload, returncode=0):
    return lambda *args, **kwargs: SimpleNamespace(
        stdout=json.dumps(payload), stderr="", returncode=returncode,
    )


def _fallback_runner(payload):
    def run(command, **kwargs):
        if command[-1] == "--version" and command[0].endswith("node"):
            return SimpleNamespace(stdout="v22.0.0\n", stderr="", returncode=0)
        return SimpleNamespace(stdout=json.dumps(payload), stderr="", returncode=0)
    return run


@pytest.mark.parametrize("version", ["0.4.9", "0.5.1", "0.9.0-rc.1", "1.2.3", "99.0.0"])
def test_any_version_of_the_right_bin_is_accepted(version):
    probe = _probe(
        "/tools/woltspace-tui",
        runner=_runner({"name": TUI_PACKAGE, "version": version, "binary": TUI_BINARY}),
    )
    assert probe["valid"] is True
    assert probe["version"] == version
    assert "error" not in probe


def test_another_package_wearing_the_bin_name_is_refused():
    probe = _probe(
        "/tools/woltspace-tui",
        runner=_runner({"name": "tui", "version": "0.5.1", "binary": TUI_BINARY}),
    )
    assert probe["valid"] is False
    assert TUI_PACKAGE in probe["error"]


def test_the_tui_bin_does_not_pass_for_the_service_bin():
    """Same package, right version, wrong bin — identity is not a substring."""
    probe = _probe(
        "/tools/woltspace-tui-service",
        expected_binary=TUI_SERVICE_BINARY,
        runner=_runner({"name": TUI_PACKAGE, "version": __version__, "binary": TUI_BINARY}),
    )
    assert probe["valid"] is False
    assert TUI_SERVICE_BINARY in probe["error"]


def test_a_binary_that_answers_nothing_useful_is_refused():
    probe = _probe("/tools/woltspace-tui", runner=_runner({}, returncode=1))
    assert probe["valid"] is False
    assert "unknown" in probe["error"]


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def test_a_local_binary_is_preferred_whatever_its_version():
    resolution = resolve_tui(
        {},
        which=lambda name: "/tools/woltspace-tui" if name == TUI_BINARY else "/tools/npx",
        runner=_runner({"name": TUI_PACKAGE, "version": "0.4.2", "binary": TUI_BINARY}),
    )
    assert resolution.source == "local"
    assert resolution.command == ("/tools/woltspace-tui",)
    record = resolution.to_record()
    assert record["version"] == "0.4.2", "the record reports what resolved, not a pin"
    assert record["spec"] == tui_spec()


def test_a_foreign_local_binary_falls_back_to_npx_latest():
    resolution = resolve_tui(
        {},
        which=lambda name: f"/tools/{name}",
        runner=_fallback_runner({"name": "tui", "version": "0.2.1", "binary": TUI_BINARY}),
    )
    assert resolution.source == "npx"
    assert resolution.command == (
        "/tools/npx", "--yes", "--package=@woltspace/tui@latest", "woltspace-tui",
    )
    assert resolution.local_probe["name"] == "tui"
    assert resolution.to_record()["version"] is None


def test_missing_binary_and_npx_has_actionable_remedy():
    with pytest.raises(TuiResolutionError, match="Install Node.js 18 or newer"):
        resolve_tui({}, which=lambda name: None)


def test_old_node_is_named_before_the_npx_launch():
    def which(name):
        return f"/tools/{name}" if name in {"node", "npx"} else None

    with pytest.raises(TuiResolutionError, match="Found Node.js 16"):
        resolve_tui(
            {}, which=which,
            runner=lambda *a, **kw: SimpleNamespace(
                stdout="v16.20.0\n", stderr="", returncode=0,
            ),
        )
