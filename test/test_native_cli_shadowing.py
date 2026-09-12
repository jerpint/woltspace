"""The name `woltspace` must not mean "boot Docker" on a native colony.

Two scripts in this repo answer to `woltspace` and neither of them is the CLI a
native user installed:

  * `container/bin/woltspace` — the thin HTTP control client. It ships inside
    the wheel's `_bundle`, and that bundle's `container/bin` is prepended to
    every native session's PATH so `notify` and `push-view` resolve. The client
    rides along and shadows the console script.
  * `woltspace` at the root of this checkout — the container-era bash launcher,
    which drives Docker.

Together they made a live accident: `woltspace start` on a native mac hit the
bundled client, which said "run it on the host where docker lives", and the
launcher that answers to that advice booted a whole second colony on the same
data root, port, and Telegram token.

These cover both seams: the client delegates instead of misdirecting, and the
launcher refuses to boot over a live native colony.
"""

import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "container" / "bin" / "woltspace"
LAUNCHER = ROOT / "woltspace"


# Every probe runs with a PATH that cannot see the real install. A test that
# leaks the host's PATH does not just test the wrong thing — it dispatches
# lifecycle verbs at the colony the developer is standing in.
SYSTEM_PATH = f"/usr/bin{os.pathsep}/bin"


def _clean_env(tmp_path=None, **extra):
    """The ambient environment minus every woltspace path variable.

    HOME is redirected too, whenever the caller has a tmp_path to give. The
    guards in these probes are what keep a launcher from reaching `_setup_path`
    — which appends to `~/.zshrc` / `~/.bash_profile` after a
    `read -r _confirm </dev/tty` — and a test should not be one regression away
    from editing the developer's shell rc.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("WOLTS_", "WOLTSPACE_", "WOLT_"))
    }
    env["PATH"] = SYSTEM_PATH
    if tmp_path is not None:
        home = Path(tmp_path) / "home"
        home.mkdir(parents=True, exist_ok=True)
        env["HOME"] = str(home)
    env.update(extra)
    return env


# --------------------------------------------------------------------------
# The bundled control client
# --------------------------------------------------------------------------

def _fake_bundle(tmp_path, *, cli_body=None, interpreter=True):
    """A venv-shaped install: <venv>/lib/pythonX.Y/site-packages/woltspace/_bundle.

    The client's whole point is that it can find its own sibling console script
    by arithmetic on its path, so the test has to reproduce the real shape —
    and "the real shape" includes `container/lib` beside `container/bin`. The
    client imports from there (`env_compat`), resolving it relative to its own
    location, so a fake bundle holding only the script is not a bundle the
    client can run in.
    """
    venv = tmp_path / "venv"
    site_packages = venv / "lib" / "python3.13" / "site-packages"
    bundle = site_packages / "woltspace" / "_bundle"
    bin_dir = bundle / "container" / "bin"
    bin_dir.mkdir(parents=True)
    client = bin_dir / "woltspace"
    client.write_text(CLIENT.read_text())
    client.chmod(0o755)

    # Mirror the wheel: whatever the client imports out of `container/lib` has
    # to be there. Copied by discovery rather than by name, and tolerantly, so
    # this fixture is correct both before and after the modules land.
    repo_lib = CLIENT.resolve().parents[1] / "lib"
    fake_lib = bundle / "container" / "lib"
    fake_lib.mkdir(parents=True, exist_ok=True)
    for module in ("env_compat.py", "notify_prompt.py"):
        source = repo_lib / module
        if source.exists():
            (fake_lib / module).write_text(source.read_text())

    (venv / "bin").mkdir(parents=True)
    if interpreter:
        script = venv / "bin" / "woltspace"
        script.write_text(cli_body or (
            "#!/bin/sh\n"
            'printf \'{"native": true, "argv": "%s"}\\n\' "$*"\n'
        ))
        script.chmod(0o755)
    return client, venv


def test_native_lifecycle_verb_execs_the_console_script_beside_the_bundle(tmp_path):
    client, venv = _fake_bundle(tmp_path)
    result = subprocess.run(
        [sys.executable, str(client), "start", "--port", "8080"],
        capture_output=True, text=True, timeout=30,
        env=_clean_env(tmp_path, WOLTSPACE_ISOLATION="host"),
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["native"] is True
    assert payload["argv"] == "start --port 8080"
    # And emphatically not the old misdirection.
    assert "docker lives" not in result.stderr


@pytest.mark.parametrize(
    "verb", ["doctor", "paths", "serve", "restore", "--version", "--help", "-h"])
def test_native_delegates_every_verb_it_does_not_serve(verb, tmp_path):
    """`woltspace doctor` used to die on an argparse error. It is the CLI's now.

    `--help` is in the list for the same reason: the bundle is first on a
    session's PATH, so its four-noun help page was the only help a native user
    could see — no start, no doctor, no backup, as though they did not exist.
    """
    client, _ = _fake_bundle(tmp_path)
    result = subprocess.run(
        [sys.executable, str(client), verb],
        capture_output=True, text=True, timeout=30,
        env=_clean_env(tmp_path, WOLTSPACE_ISOLATION="host"),
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["argv"] == verb
    assert "invalid choice" not in result.stderr


def test_bare_invocation_natively_shows_the_real_cli(tmp_path):
    """No verb at all is still a question about the whole CLI."""
    client, _ = _fake_bundle(tmp_path)
    result = subprocess.run(
        [sys.executable, str(client)],
        capture_output=True, text=True, timeout=30,
        env=_clean_env(tmp_path, WOLTSPACE_ISOLATION="host"),
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["argv"] == "--help"


def test_container_help_stays_with_the_thin_client(tmp_path):
    """In the container the rest of the CLI really does live on the host, so
    the client's own help is the honest answer and nothing is delegated."""
    client, _ = _fake_bundle(tmp_path)
    result = subprocess.run(
        [sys.executable, str(client), "--help"],
        capture_output=True, text=True, timeout=30,
        env=_clean_env(tmp_path, WOLTSPACE_ISOLATION="external"),
    )
    assert result.returncode == 0, result.stderr
    assert "native" not in result.stdout
    assert "session" in result.stdout


def test_native_refuses_loudly_when_no_console_script_can_be_found(tmp_path):
    """No silent fallthrough, and no pointer at the docker launcher."""
    client, _ = _fake_bundle(tmp_path, interpreter=False)
    empty_bin = tmp_path / "empty"
    empty_bin.mkdir()
    result = subprocess.run(
        [sys.executable, str(client), "start"],
        capture_output=True, text=True, timeout=30,
        env=_clean_env(tmp_path, WOLTSPACE_ISOLATION="host", PATH=str(empty_bin)),
    )
    assert result.returncode == 2
    assert "belongs to the native woltspace CLI" in result.stderr
    assert "uv tool install woltspace" in result.stderr
    assert "second colony" in result.stderr


def test_path_fallback_skips_the_bash_launcher(tmp_path):
    """A launcher-shaped `woltspace` first on PATH must never receive our argv.

    This is the ordering the person who hit this had: the container-era bash
    script ahead of `~/.local/bin`.
    """
    client, _ = _fake_bundle(tmp_path, interpreter=False)

    bash_first = tmp_path / "checkout"
    bash_first.mkdir()
    launcher = bash_first / "woltspace"
    launcher.write_text("#!/bin/bash\necho 'BOOTED DOCKER'\n")
    launcher.chmod(0o755)

    real = tmp_path / "local-bin"
    real.mkdir()
    console = real / "woltspace"
    console.write_text("#!/usr/bin/env python3\nprint('NATIVE CLI')\n")
    console.chmod(0o755)

    result = subprocess.run(
        [sys.executable, str(client), "start"],
        capture_output=True, text=True, timeout=30,
        env=_clean_env(
            tmp_path,
            WOLTSPACE_ISOLATION="host",
            PATH=f"{bash_first}{os.pathsep}{real}{os.pathsep}{SYSTEM_PATH}",
        ),
    )
    assert result.returncode == 0, result.stderr
    assert "NATIVE CLI" in result.stdout
    assert "BOOTED DOCKER" not in result.stdout


def test_container_keeps_the_host_lifecycle_message(tmp_path):
    """Container mode is unchanged — same text, same exit code, no exec."""
    client, _ = _fake_bundle(tmp_path)
    result = subprocess.run(
        [sys.executable, str(client), "start"],
        capture_output=True, text=True, timeout=30,
        env=_clean_env(tmp_path, WOLTSPACE_ISOLATION="external"),
    )
    assert result.returncode == 2
    assert "is a host lifecycle command" in result.stderr
    assert "docker lives" in result.stderr
    assert result.stdout == ""


def test_container_is_detected_from_mount_points_without_any_environment(tmp_path):
    """A bare `docker exec` shell carries no WOLTSPACE_ISOLATION.

    The bundle path cannot answer this — the image symlinks
    /workspace/woltspace at the very same `_bundle` — so the fixed mount points
    have to. Skipped off a container, where they do not exist.
    """
    if not (os.path.isdir("/workspace/woltspace") and os.path.isdir("/workspace/wolts")):
        pytest.skip("not running inside the woltspace image")
    client, _ = _fake_bundle(tmp_path)
    result = subprocess.run(
        [sys.executable, str(client), "start"],
        capture_output=True, text=True, timeout=30, env=_clean_env(tmp_path),
    )
    assert result.returncode == 2
    assert "is a host lifecycle command" in result.stderr


def test_control_nouns_stay_local_and_never_delegate(tmp_path):
    """`session send` is how wolts talk to each other; it must keep working.

    The native CLI has no `session` noun at all, so delegating it would break
    IWCL on every native colony.
    """
    client, _ = _fake_bundle(tmp_path)
    result = subprocess.run(
        [sys.executable, str(client), "session", "list"],
        capture_output=True, text=True, timeout=30,
        env=_clean_env(
            tmp_path,
            WOLTSPACE_ISOLATION="host",
            WOLTSPACE_API="http://127.0.0.1:1",  # nothing listening
        ),
    )
    # Handled here: it tried to reach the API rather than exec'ing the CLI.
    assert result.returncode == 2
    assert "cannot reach woltspace API" in result.stderr


# --------------------------------------------------------------------------
# The container-era bash launcher
# --------------------------------------------------------------------------

def _owner_record(wolts_dir, **fields):
    platform = wolts_dir / ".space" / "platform"
    platform.mkdir(parents=True, exist_ok=True)
    record = {
        "instance_id": "af6ee123774a427488d39151aacfab27",
        "pid": os.getpid(),
        "started_at": 1788757804,
        "endpoint": "http://127.0.0.1:7777",
        "isolation": "host",
        "hostname": "test-host",
    }
    record.update(fields)
    (platform / "control-plane.json").write_text(json.dumps(record, indent=2))


def _lodge(tmp_path):
    """A wolts dir with one wolt in it, so the launcher's own gate lets us in."""
    wolts_dir = tmp_path / "wolts"
    (wolts_dir / "testwolt" / "wolt").mkdir(parents=True)
    return wolts_dir


@contextmanager
def _control_plane_decoy():
    """A live process shaped like the native control plane, and its pid.

    The refusal tests used to record `os.getpid()` — pytest's own pid — and so
    passed only while pytest's argv happened to satisfy the guard's heuristic.
    That is an accidental pass: run the same suite from a directory whose path
    does not contain "woltspace" and the guard correctly declines to recognise
    the recorder, and every refusal test fails for a reason that has nothing to
    do with the code under test. Spawn the shape explicitly instead.
    """
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", "woltspace", "serve"]
    )
    try:
        yield process.pid
    finally:
        process.terminate()
        process.wait(timeout=10)


def _launch(wolts_dir, verb="start"):
    fake_bin = wolts_dir.parent / "fake-bin"
    fake_bin.mkdir(exist_ok=True)
    # A docker that screams if the guard ever lets it through.
    docker = fake_bin / "docker"
    docker.write_text("#!/bin/sh\necho 'DOCKER INVOKED' >&2\nexit 0\n")
    docker.chmod(0o755)
    # Both spellings of the data-root variable. The canonical name is what the
    # launcher prefers; the legacy one is still honoured, and setting both keeps
    # this correct on either side of the env-namespace rename.
    return subprocess.run(
        ["bash", str(LAUNCHER), verb],
        capture_output=True, text=True, timeout=60,
        env=_clean_env(
            wolts_dir.parent,
            WOLTSPACE_WOLTS_DIR=str(wolts_dir),
            WOLTS_DIR=str(wolts_dir),
            PATH=f"{fake_bin}{os.pathsep}{SYSTEM_PATH}",
        ),
    )


@pytest.mark.parametrize("verb", ["start", "rebuild", "init"])
def test_launcher_refuses_to_boot_over_a_live_native_colony(verb, tmp_path):
    wolts_dir = _lodge(tmp_path)
    with _control_plane_decoy() as pid:
        _owner_record(wolts_dir, pid=pid)
        result = _launch(wolts_dir, verb)
    assert result.returncode == 1, result.stdout
    assert "this clearing is already taken" in result.stdout
    assert "two bots on one token" in result.stdout
    assert str(wolts_dir) in result.stdout
    # Prevented, not warned about: docker never ran.
    assert "DOCKER INVOKED" not in result.stderr


def test_launcher_refusal_points_at_the_native_cli_and_an_escape_hatch(tmp_path):
    wolts_dir = _lodge(tmp_path)
    with _control_plane_decoy() as pid:
        _owner_record(wolts_dir, pid=pid)
        out = _launch(wolts_dir).stdout
    assert "woltspace stop" in out
    assert "which -a woltspace" in out
    assert "WOLTS_DIR=" in out


def test_launcher_still_boots_when_the_owner_is_a_container(tmp_path):
    """Container mode unchanged: an "external" owner is this script's own work."""
    wolts_dir = _lodge(tmp_path)
    _owner_record(wolts_dir, isolation="external")
    result = _launch(wolts_dir)
    assert "this clearing is already taken" not in result.stdout


def test_launcher_ignores_a_stale_native_record(tmp_path):
    """A dead pid must not wedge the launcher forever."""
    wolts_dir = _lodge(tmp_path)
    _owner_record(wolts_dir, pid=2 ** 22)  # above any pid_max, cannot be alive
    result = _launch(wolts_dir)
    assert "this clearing is already taken" not in result.stdout


def test_launcher_ignores_a_recycled_pid(tmp_path):
    """A live pid that is not a woltspace must not block a legitimate start.

    Pids get recycled. `kill -0` says only "something answers to this number",
    so a stale record naming a since-recycled pid — a shell, an editor — used
    to refuse the boot with a message about a colony that is not there, and no
    way for the human to tell from the text that it was wrong.
    """
    wolts_dir = _lodge(tmp_path)
    # A shell, not a control plane — and one whose command line *does* contain
    # the word "woltspace", because on a developer machine every path does: the
    # checkout, the data root, the worktrees. A plain substring match on argv
    # was the second half of this bug, and it refused exactly this process.
    decoy = subprocess.Popen(
        ["sh", "-c", "sleep 30  # /Users/someone/.woltspace/wolts/uxwolt"]
    )
    try:
        _owner_record(wolts_dir, pid=decoy.pid)
        result = _launch(wolts_dir)
        assert "this clearing is already taken" not in result.stdout, (
            "a recycled pid must not be mistaken for a running colony"
        )
    finally:
        decoy.terminate()
        decoy.wait(timeout=10)


@pytest.mark.parametrize(
    "argv_tail,why",
    [
        (["woltspace", "serve"], "the control plane's own invocation shape"),
        (["--config", "/Users/x/.woltspace/wolts"], "a python that is about woltspace"),
    ],
)
def test_launcher_still_refuses_for_a_live_control_plane(argv_tail, why, tmp_path):
    """The positive cases. Narrowing the check must not narrow it past these.

    The native control plane runs `<python> -m woltspace serve …`, so both
    signals are present in the first case. The second is the deliberate
    leniency: a python whose command line mentions woltspace counts, because a
    missed refusal costs a double boot and a spurious one only costs a retry.
    """
    wolts_dir = _lodge(tmp_path)
    plane = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", *argv_tail]
    )
    try:
        _owner_record(wolts_dir, pid=plane.pid)
        result = _launch(wolts_dir)
        assert result.returncode == 1, why
        assert "this clearing is already taken" in result.stdout, why
    finally:
        plane.terminate()
        plane.wait(timeout=10)


def test_launcher_ignores_a_missing_record(tmp_path):
    wolts_dir = _lodge(tmp_path)
    result = _launch(wolts_dir)
    assert "this clearing is already taken" not in result.stdout


# --------------------------------------------------------------------------
# Why the shadowing happens at all
# --------------------------------------------------------------------------

def test_session_path_still_carries_the_bundle_bin(tmp_path):
    """The prepend stays — `notify` and `push-view` depend on it.

    The fix is that the client at the front of that directory now behaves like
    the CLI it shadows, not that we stop shipping the directory.
    """
    sys.path.insert(0, str(ROOT / "container" / "lib"))
    from session_runtime import _session_env_value  # noqa: E402

    install_root = tmp_path / "install"
    (install_root / "container" / "bin").mkdir(parents=True)
    os.environ["WOLTSPACE_DIR"] = str(install_root)
    try:
        adjusted = _session_env_value("PATH", "/usr/bin")
        assert adjusted.startswith(str(install_root / "container" / "bin"))
        # Idempotent — copies must not stack across restarts.
        assert _session_env_value("PATH", adjusted) == adjusted
    finally:
        os.environ.pop("WOLTSPACE_DIR", None)
