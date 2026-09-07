"""The WOLTSPACE_* env namespace: resolution, deprecation, both-name exports.

Nothing here reads the real colony. Every case builds its own mapping and hands
it in, or monkeypatches `os.environ` — a test that saw the developer's
`WOLTS_DIR` would pass or fail depending on whose machine ran it.
"""

from __future__ import annotations

import io
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "container" / "lib"))

import env_compat  # noqa: E402
from woltspace import envvars  # noqa: E402

# The pair under test everywhere below; the table drives the rest.
NEW = "WOLTSPACE_WOLTS_DIR"
OLD = "WOLTS_DIR"

MODULES = pytest.mark.parametrize(
    "mod", [envvars, env_compat], ids=["woltspace.envvars", "container.env_compat"]
)


@pytest.fixture(autouse=True)
def _fresh_warning_state():
    """Each test starts as a process that has never warned."""
    for mod in (envvars, env_compat):
        mod._reset_warning_state()
    yield
    for mod in (envvars, env_compat):
        mod._reset_warning_state()


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

@MODULES
def test_canonical_name_wins_over_legacy(mod):
    env = {NEW: "/new", OLD: "/old"}
    assert mod.get_env(NEW, env=env) == "/new"


@MODULES
def test_legacy_name_is_the_fallback(mod):
    assert mod.get_env(NEW, env={OLD: "/old"}) == "/old"


@MODULES
def test_default_when_neither_is_set(mod):
    assert mod.get_env(NEW, "/fallback", env={}) == "/fallback"


@MODULES
def test_empty_canonical_does_not_shadow_a_real_legacy_value(mod):
    """An exported-but-empty variable is not a value.

    tmux and docker both hand children `VAR=` for a variable that was never
    given one; treating that as set would make the empty string beat the legacy
    path a user actually configured.
    """
    assert mod.get_env(NEW, "/fallback", env={NEW: "", OLD: "/old"}) == "/old"


@MODULES
def test_explicit_legacy_overrides_the_table(mod):
    assert mod.get_env(NEW, legacy="SOMETHING_ELSE", env={"SOMETHING_ELSE": "/x"}) == "/x"


@MODULES
def test_reads_os_environ_when_no_mapping_is_given(mod, monkeypatch):
    monkeypatch.delenv(NEW, raising=False)
    monkeypatch.setenv(OLD, "/from-process-env")
    assert mod.get_env(NEW) == "/from-process-env"


@MODULES
def test_every_legacy_name_resolves_through_its_canonical_name(mod):
    for canonical, legacy in mod.LEGACY_ALIASES.items():
        assert mod.get_env(canonical, env={legacy: "value"}) == "value"


# ---------------------------------------------------------------------------
# Deprecation warning
# ---------------------------------------------------------------------------

@MODULES
def test_warning_fires_once_per_process_not_once_per_read(mod):
    env = {OLD: "/old"}
    stream = io.StringIO()
    assert mod.warn_legacy_once(env=env, stream=stream) == [OLD]
    for _ in range(5):
        mod.get_env(NEW, env=env)
        assert mod.warn_legacy_once(env=env, stream=stream) == []
    assert stream.getvalue().count("deprecated env var") == 1


@MODULES
def test_warning_names_the_replacement_and_the_sunset(mod):
    stream = io.StringIO()
    mod.warn_legacy_once(env={OLD: "/old"}, stream=stream)
    assert stream.getvalue().strip() == (
        f"deprecated env var {OLD} — use {NEW} (legacy names honored until 1.0)"
    )


@MODULES
def test_no_warning_when_only_canonical_names_are_set(mod):
    stream = io.StringIO()
    assert mod.warn_legacy_once(env={NEW: "/new"}, stream=stream) == []
    assert stream.getvalue() == ""


@MODULES
def test_a_legacy_name_beside_its_canonical_twin_is_not_in_use(mod):
    """Both-name exports must not make the platform warn about itself.

    Everything that spawns a child sets both spellings, so the child's
    environment always holds the legacy name. Warning on its mere presence
    would fire on every session, for every user, forever.
    """
    assert mod.legacy_in_use({NEW: "/new", OLD: "/new"}) == []


@MODULES
def test_a_fallback_get_env_took_is_reported_even_if_the_env_changes(mod):
    mod.get_env(NEW, env={OLD: "/old"})
    assert OLD in mod.legacy_in_use({})


@MODULES
def test_get_env_warns_on_its_own_first_fallback(mod, monkeypatch, capsys):
    # A developer's own shell may carry several legacy names; the warning is
    # about this pair, so the rest are cleared rather than counted.
    for canonical, legacy in mod.LEGACY_ALIASES.items():
        monkeypatch.delenv(canonical, raising=False)
        monkeypatch.delenv(legacy, raising=False)
    monkeypatch.setenv(OLD, "/old")
    mod.get_env(NEW)
    mod.get_env(NEW)
    assert capsys.readouterr().err.count("deprecated env var") == 1


# ---------------------------------------------------------------------------
# Both-name exports
# ---------------------------------------------------------------------------

@MODULES
def test_export_both_mirrors_a_canonical_value_onto_the_legacy_name(mod):
    assert mod.export_both({NEW: "/root"})[OLD] == "/root"


@MODULES
def test_export_both_mirrors_a_legacy_value_onto_the_canonical_name(mod):
    assert mod.export_both({OLD: "/root"})[NEW] == "/root"


@MODULES
def test_export_both_prefers_canonical_when_both_are_given(mod):
    both = mod.export_both({NEW: "/new", OLD: "/old"})
    assert both[NEW] == both[OLD] == "/new"


@MODULES
def test_export_both_leaves_absent_pairs_absent(mod):
    both = mod.export_both({"UNRELATED": "x"})
    assert both == {"UNRELATED": "x"}


@MODULES
def test_export_both_does_not_mutate_its_input(mod):
    source = {NEW: "/root"}
    mod.export_both(source)
    assert source == {NEW: "/root"}


@MODULES
def test_every_pair_is_mirrored(mod):
    for canonical, legacy in mod.LEGACY_ALIASES.items():
        assert mod.export_both({canonical: "v"})[legacy] == "v"
        assert mod.export_both({legacy: "v"})[canonical] == "v"


# ---------------------------------------------------------------------------
# The two copies of the helper
# ---------------------------------------------------------------------------

SHARED_START = "# --- BEGIN SHARED BLOCK ---"
SHARED_END = "# --- END SHARED BLOCK ---"

MIRRORED = (
    REPO / "src" / "woltspace" / "envvars.py",
    REPO / "container" / "lib" / "env_compat.py",
)


def _shared_block(path: Path) -> str:
    text = path.read_text()
    assert SHARED_START in text and SHARED_END in text, f"{path} lost its markers"
    return text.split(SHARED_START, 1)[1].split(SHARED_END, 1)[0]


def test_the_two_copies_of_the_helper_have_not_drifted():
    """`container/lib` and the `woltspace` package cannot import each other.

    `container/lib` is a flat, stdlib-only PYTHONPATH tree, and
    `layout.py` is what puts it on the path — so the package cannot depend on
    it at import time. The helper is therefore mirrored, and this is what keeps
    the mirror honest.
    """
    first, second = (_shared_block(path) for path in MIRRORED)
    assert first == second


def test_the_alias_table_covers_exactly_the_renamed_variables():
    assert envvars.LEGACY_ALIASES == {
        "WOLTSPACE_WOLTS_DIR": "WOLTS_DIR",
        "WOLTSPACE_WOLT_DIR": "WOLT_DIR",
        "WOLTSPACE_WOLT_NAME": "WOLT_NAME",
        "WOLTSPACE_WOLT_SESSION": "WOLT_SESSION",
    }


# ---------------------------------------------------------------------------
# Nothing reads a legacy name behind the helper's back
# ---------------------------------------------------------------------------

#: Reading a legacy name directly, in python. Writing one is fine — that is the
#: both-name export every child depends on.
LEGACY = "WOLTS_DIR|WOLT_DIR|WOLT_NAME|WOLT_SESSION"
DIRECT_READ = re.compile(
    r"""(?:os\.environ|environ|values|source|env)\s*
        (?:\.get\(\s*|\[\s*)
        ["'](?:""" + LEGACY + r""")["']""",
    re.VERBOSE,
)
#: `os.environ["WOLT_NAME"] = ...` — a both-name export, which is the point.
WRITE = re.compile(r"""\[\s*["'](?:""" + LEGACY + r""")["']\s*\]\s*=""")

#: The helper itself is where the legacy names are allowed to be named.
EXEMPT = {
    "src/woltspace/envvars.py",
    "container/lib/env_compat.py",
    "test/test_env_namespace.py",
}


def _tracked_python_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "src", "server", "container", "tui", "test"],
        cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout.split()
    return [
        path for path in out
        if (path.endswith(".py") or "/bin/" in path) and path not in EXEMPT
    ]


def test_no_module_reads_a_legacy_env_name_directly():
    """Every read goes through `get_env`; only exports name the old spelling."""
    offenders = []
    for path in _tracked_python_files():
        full = REPO / path
        if not full.is_file():
            continue
        try:
            text = full.read_text()
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if DIRECT_READ.search(line) and not WRITE.search(line):
                offenders.append(f"{path}:{number}: {line.strip()}")
    assert offenders == [], "read these through get_env instead:\n" + "\n".join(offenders)


def test_sessions_inherit_both_spellings_of_every_renamed_variable():
    """A wolt skill reading `$WOLT_NAME` must still find it in a fresh session."""
    import session_runtime

    for canonical, legacy in env_compat.LEGACY_ALIASES.items():
        assert canonical in session_runtime._SESSION_ENV_KEYS
        assert legacy in session_runtime._SESSION_ENV_KEYS


def test_launch_command_carries_a_legacy_only_value_under_both_names(monkeypatch):
    """The mirror happens at spawn, not just at control-plane startup.

    A bot process handed only `WOLT_NAME` still has to hand its child both.
    """
    import session_runtime

    monkeypatch.delenv("WOLTSPACE_WOLT_NAME", raising=False)
    monkeypatch.setenv("WOLT_NAME", "testwolt")
    command = session_runtime.TmuxSessionRuntime._launch_command("run-session.sh x")
    assert "WOLTSPACE_WOLT_NAME=testwolt" in command
    assert "WOLT_NAME=testwolt" in command
