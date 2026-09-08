"""The WOLTSPACE_* env namespace: resolution, deprecation, both-name exports.

Nothing here reads the real colony. Every case builds its own mapping and hands
it in, or monkeypatches `os.environ` — a test that saw the developer's
`WOLTS_DIR` would pass or fail depending on whose machine ran it.
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
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


def _tracked(*paths: str) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", *paths],
        cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout.split()
    return [path for path in out if path not in EXEMPT]


def _python_sources() -> list[str]:
    return [
        path for path in _tracked("src", "server", "container", "tui", "test")
        if path.endswith(".py") or "/bin/" in path
    ]


def test_no_python_module_reads_a_legacy_env_name_directly():
    """Python reads go through `get_env`; only exports name the old spelling.

    Python only — the sibling test below covers shell and JavaScript, which
    cannot be checked this precisely. See `docs/environment.md`.
    """
    offenders = []
    for path in _python_sources():
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


# ---------------------------------------------------------------------------
# Shell and JavaScript
# ---------------------------------------------------------------------------

#: `$WOLT_NAME` in bash is the same syntax whether the variable came from the
#: environment or from an assignment three lines up, and `WOLT_NAME` in JS is
#: the same token in `process.env.WOLT_NAME` as in a `const`. So these files
#: get a weaker but honest contract instead of a read/write distinction: a
#: legacy name may appear, but never *alone* — the canonical name has to be in
#: the same file, which is what the `${WOLTSPACE_X:-${X:-…}}` fallback shape
#: and the both-name exports both produce. A new script that reads only the old
#: spelling fails this.
LEGACY_TOKEN = re.compile(r"(?<![A-Z0-9_])(?:" + LEGACY + r")(?![A-Z0-9_])")
CANONICAL_TOKEN = re.compile(r"WOLTSPACE_(?:" + LEGACY + r")")

SCRIPT_SUFFIXES = (".sh", ".bash", ".js", ".mjs", ".cjs", ".md")

#: Migration guides and scripts are the record of what a *released* version
#: did. Rewriting them would falsify history, and
#: `container/migrations/v0.5.0.sh` takes its wolts directory as a positional
#: argument rather than from the environment at all.
HISTORICAL = ("container/migrations/", "docs/migrations/")


def _script_sources() -> list[str]:
    return [
        path for path in _tracked()
        if not path.startswith(HISTORICAL)
        and (
            path.endswith(SCRIPT_SUFFIXES)
            or path == "woltspace"
            or ("/bin/" in path and not path.endswith(".py"))
        )
    ]


def test_no_shell_or_js_file_names_a_legacy_env_var_alone():
    """A legacy name in a script always sits beside its canonical name."""
    offenders = []
    for path in _script_sources():
        full = REPO / path
        if not full.is_file():
            continue
        try:
            text = full.read_text()
        except UnicodeDecodeError:
            continue
        if CANONICAL_TOKEN.search(text):
            continue
        # Blank the canonical spellings first, or `WOLTSPACE_WOLT_NAME` would
        # register as a match for `WOLT_NAME`.
        if LEGACY_TOKEN.search(CANONICAL_TOKEN.sub("", text)):
            offenders.append(path)
    assert offenders == [], (
        "these name a legacy env var with no canonical name in sight:\n"
        + "\n".join(offenders)
    )


# ---------------------------------------------------------------------------
# `server.config` keeps its two env readers apart
# ---------------------------------------------------------------------------

def test_server_config_does_not_shadow_the_namespace_helper():
    """Two readers, two names, neither rebinding the other.

    `server/config.py` reads paths through the namespace helper AND owns a
    `.env`-fallback reader for other platforms' credentials. Both were once
    called `get_env`, and the second definition rebound the first — so the
    name `server.config.get_env` that `notify.py` imports was the dotenv one,
    and a later call there asking for a renamed variable would have lost its
    legacy fallback silently.
    """
    from server import config

    assert config.resolve_env is env_compat.get_env
    assert config.resolve_env is not config.dotenv_env
    assert not hasattr(config, "get_env"), "`get_env` is ambiguous in this module"


def test_the_namespace_helper_in_server_config_still_resolves_legacy_names():
    from server import config

    assert config.resolve_env("WOLTSPACE_WOLTS_DIR", env={"WOLTS_DIR": "/old"}) == "/old"
    assert config.resolve_env(
        "WOLTSPACE_WOLTS_DIR", env={"WOLTSPACE_WOLTS_DIR": "/new", "WOLTS_DIR": "/old"}
    ) == "/new"


def test_the_dotenv_reader_in_server_config_takes_a_bare_key(monkeypatch):
    """No legacy fallback here on purpose — these names belong to other platforms.

    `load_dotenv` is stubbed rather than allowed to read the developer's own
    wolt `.env`: a test must never depend on what is in the live colony.
    """
    from server import config

    monkeypatch.setattr(config, "load_dotenv", lambda: {"TELEGRAM_BOT_TOKEN": "from-dotenv"})
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert config.dotenv_env("TELEGRAM_BOT_TOKEN") == "from-dotenv"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "from-process-env")
    assert config.dotenv_env("TELEGRAM_BOT_TOKEN") == "from-process-env"
    assert config.dotenv_env("A_NAME_NOTHING_SETS") == ""


def test_notify_reads_other_platforms_credentials_through_the_dotenv_reader():
    from server import notify

    assert notify.dotenv_env is not None
    assert not hasattr(notify, "get_env")


# ---------------------------------------------------------------------------
# Naming a wolt to CREATE is not naming the wolt you are IN
# ---------------------------------------------------------------------------

def _init_name_block() -> str:
    """The host launcher's non-interactive first-wolt resolution, as bash."""
    source = (REPO / "woltspace").read_text()
    start = source.index('    NAME="${WOLTSPACE_INIT_WOLT_NAME:-}"')
    end = source.index('    [ -n "$NAME" ] && echo', start)
    return '_D=""; _R=""\n' + source[start:end] + '\necho "NAME=$NAME"\n'


def _resolve_init_name(tmp_path, **env) -> tuple[str, str]:
    """Run that block under a given environment. Returns (name, stderr)."""
    script = tmp_path / "init-name.sh"
    script.write_text(_init_name_block())
    result = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True,
        # A bare environment: the developer's own shell is inside a wolt, and
        # its ambient WOLTSPACE_WOLT_NAME is exactly what these cases are about.
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **env},
    )
    name = ""
    for line in result.stdout.splitlines():
        if line.startswith("NAME="):
            name = line[len("NAME="):]
    return name, result.stderr


def test_the_init_time_name_is_its_own_variable(tmp_path):
    name, stderr = _resolve_init_name(tmp_path, WOLTSPACE_INIT_WOLT_NAME="alpha")
    assert name == "alpha"
    assert stderr == ""


def test_an_existing_install_script_still_works_and_is_told_the_new_name(tmp_path):
    """Scripted installs that predate the split keep working."""
    name, stderr = _resolve_init_name(tmp_path, WOLTSPACE_WOLT_NAME="beta")
    assert name == "beta"
    assert "WOLTSPACE_INIT_WOLT_NAME" in stderr


def test_the_ambient_name_from_a_wolt_session_never_names_a_new_wolt(tmp_path):
    """The hazard the both-name exports created.

    `WOLTSPACE_WOLT_NAME` now reaches every session, cron and worktree shell
    carrying "the wolt I belong to". A scripted `init` launched from one of
    those shells must not read that as "create a wolt called uxwolt".
    """
    name, stderr = _resolve_init_name(
        tmp_path, WOLTSPACE_WOLT_NAME="uxwolt", WOLTSPACE_WOLT_SESSION="some-session",
    )
    assert name == ""
    assert "ignoring WOLTSPACE_WOLT_NAME" in stderr


def test_the_new_name_wins_over_an_ambient_one(tmp_path):
    name, _ = _resolve_init_name(
        tmp_path,
        WOLTSPACE_INIT_WOLT_NAME="alpha",
        WOLTSPACE_WOLT_NAME="uxwolt",
        WOLTSPACE_WOLT_SESSION="some-session",
    )
    assert name == "alpha"


def test_neither_set_means_the_lodge_asks_later(tmp_path):
    name, stderr = _resolve_init_name(tmp_path)
    assert name == ""
    assert stderr == ""


def test_the_cli_smoke_test_uses_the_init_time_name():
    """The one in-repo consumer of the old spelling has moved over."""
    smoke = (REPO / "test" / "test-cli.sh").read_text()
    assert "WOLTSPACE_INIT_WOLT_NAME=" in smoke
    assert "WOLTSPACE_WOLT_NAME=" not in smoke


def test_the_script_scan_actually_looks_at_the_scripts_that_matter():
    """Guard the guard: a glob that quietly matches nothing proves nothing."""
    scanned = set(_script_sources())
    for path in (
        "woltspace",
        "container/bin/notify",
        "container/bin/push-view",
        "container/bin/run-session.sh",
        "container/hooks/run-session.sh",
        "container/cron/check-update.sh",
        "container/cron/digest.mjs",
        "tui/src/tui-service.js",
        "test/test-cli.sh",
        "test/run-tests.sh",
    ):
        assert path in scanned, f"{path} is not being scanned"


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
