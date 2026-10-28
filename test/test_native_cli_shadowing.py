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
import re
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
    for module in ("env_compat.py", "notify_prompt.py", "lodge_auth.py"):
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


@pytest.mark.parametrize("isolation", ["host", "external"])
def test_wolf_goes_to_the_python_cli_natively_and_in_the_container(isolation, tmp_path):
    """`woltspace wolf` only talks to the lodge API, so even inside the image
    it belongs to the installed python CLI, not to the host."""
    client, _ = _fake_bundle(tmp_path)
    result = subprocess.run(
        [sys.executable, str(client), "wolf", "list", "--json"],
        capture_output=True, text=True, timeout=30,
        env=_clean_env(tmp_path, WOLTSPACE_ISOLATION=isolation),
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"native": True, "argv": "wolf list --json"}


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


def _launch(wolts_dir, verb="start", **env):
    fake_bin = wolts_dir.parent / "fake-bin"
    fake_bin.mkdir(exist_ok=True)
    # A docker that screams if the guard ever lets it through. It logs its argv,
    # lists the container once it has been run, and answers `image inspect` of
    # the lodge's image (WOLTSPACE_IMAGE, else `woltspace`) with
    # FAKE_IMAGE_LABEL, so a `start` that is let through goes all the way to
    # `docker run`.
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/bin/sh\necho 'DOCKER INVOKED' >&2\n"
        'echo "$*" >> "$0.log"\n'
        'case "$1 $*" in\n'
        '  run*) touch "$0.ran" ;;\n'
        '  ps*) [ -f "$0.ran" ] && echo "$WOLTSPACE_CONTAINER" ;;\n'
        '  "image image inspect"*" ${WOLTSPACE_IMAGE:-woltspace}") echo "$FAKE_IMAGE_LABEL" ;;\n'
        "esac\nexit 0\n"
    )
    docker.chmod(0o755)
    (fake_bin / "sleep").write_text("#!/bin/sh\n")
    (fake_bin / "sleep").chmod(0o755)
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
            WOLTSPACE_CONTAINER="lodge-test",
            SHELL="/bin/sh",  # no rc file to offer, so no /dev/tty prompt
            PATH=f"{fake_bin}{os.pathsep}{SYSTEM_PATH}",
            **env,
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


# --------------------------------------------------------------------------
# An image built with WOLTSPACE_NODE_SUDO=false runs with no-new-privileges
# --------------------------------------------------------------------------

def _docker_calls(tmp_path, verb="start", **env):
    wolts_dir = _lodge(tmp_path)
    # No tunnel, or _show_url polls for one.
    (wolts_dir / ".env").write_text("WOLTSPACE_PUBLIC_TUNNEL=false\n")
    result = _launch(wolts_dir, verb, **env)
    log = tmp_path / "fake-bin" / "docker.log"
    calls = [c.split() for c in log.read_text().splitlines()] if log.exists() else []
    return result, calls, [c for c in calls if c[0] == "run"]


def test_an_image_labelled_without_sudo_runs_with_no_new_privileges(tmp_path):
    result, _, runs = _docker_calls(tmp_path, FAKE_IMAGE_LABEL="false")
    assert result.returncode == 0, result.stdout
    [argv] = runs
    assert argv[-1] == "woltspace"  # anything after the image is its command
    assert argv.count("no-new-privileges") == 1
    assert argv[argv.index("no-new-privileges") - 1] == "--security-opt"


@pytest.mark.parametrize("label, shell", [("true", None), ("", None), ("true", "false")])
def test_no_flag_unless_the_image_says_false(tmp_path, label, shell):
    # "" is an image from before the label; the launching shell no longer decides.
    env = {} if shell is None else {"WOLTSPACE_NODE_SUDO": shell}
    result, _, runs = _docker_calls(tmp_path, FAKE_IMAGE_LABEL=label, **env)
    assert result.returncode == 0, result.stdout
    [argv] = runs
    assert "no-new-privileges" not in argv


def test_the_dockerfile_labels_the_build_arg_the_launcher_reads():
    key = re.search(r'Labels "([^"]+)"', LAUNCHER.read_text()).group(1)
    assert f'LABEL {key}="${{WOLTSPACE_NODE_SUDO:-true}}"' in (ROOT / "container" / "Dockerfile").read_text()


def test_build_needs_no_wolts_and_only_builds(tmp_path):
    wolts_dir = tmp_path / "wolts"
    wolts_dir.mkdir()
    result = _launch(wolts_dir, "build")
    assert result.returncode == 0, result.stdout
    calls = (tmp_path / "fake-bin" / "docker.log").read_text().splitlines()
    # Reading the running container's version is fine; nothing else but the build.
    assert [c.split()[0] for c in calls if not c.endswith(" cat /workspace/woltspace/.version")] == ["build"]


def test_a_bad_node_sudo_stops_rebuild_before_docker_changes_anything(tmp_path):
    result, calls, _ = _docker_calls(tmp_path, "rebuild", WOLTSPACE_NODE_SUDO="False")
    assert result.returncode == 1
    assert "WOLTSPACE_NODE_SUDO must be true or false" in result.stderr
    assert not [c for c in calls if c[0] in ("rm", "run", "build")]


# --------------------------------------------------------------------------
# WOLTSPACE_IMAGE: each lodge on a machine builds and runs its own image
# --------------------------------------------------------------------------

@pytest.mark.parametrize("local", ["false", "true"])
def test_build_tags_the_image_woltspace_image_names(tmp_path, local):
    wolts_dir = tmp_path / "wolts"
    wolts_dir.mkdir()
    result = _launch(wolts_dir, "build", WOLTSPACE_IMAGE="lodge-b", WOLTSPACE_LOCAL=local)
    assert result.returncode == 0, result.stderr
    calls = [c.split() for c in (tmp_path / "fake-bin" / "docker.log").read_text().splitlines()]
    [build] = [c for c in calls if c[0] == "build"]
    assert build[build.index("-t") + 1] == "lodge-b"


@pytest.mark.parametrize("verb", ["start", "init"])
def test_start_inspects_and_runs_the_image_woltspace_image_names(tmp_path, verb):
    result, calls, runs = _docker_calls(tmp_path, verb, WOLTSPACE_IMAGE="lodge-b", FAKE_IMAGE_LABEL="false")
    assert result.returncode == 0, result.stdout
    [argv] = runs
    assert argv[-1] == "lodge-b"
    assert "no-new-privileges" in argv  # the label was read from lodge-b
    assert ["image", "inspect", "lodge-b"] in calls  # the "is it built yet" check
    assert not [c for c in calls if "woltspace" in c]


@pytest.mark.parametrize("verb", ["build", "start"])
def test_without_woltspace_image_the_image_is_still_woltspace(tmp_path, verb):
    _, calls, runs = _docker_calls(tmp_path, verb)
    named = [c[c.index("-t") + 1] for c in calls if c[0] == "build"] + [c[-1] for c in runs]
    assert named == ["woltspace"]


@pytest.mark.parametrize("image", ["woltspace/woltspace:latest", "registry.example:5000/lodge-b@sha256:ab12"])
def test_an_image_reference_such_as_the_desktop_apps_is_accepted(tmp_path, image):
    result, calls, runs = _docker_calls(tmp_path, WOLTSPACE_IMAGE=image, FAKE_IMAGE_LABEL="false")
    assert result.returncode == 0, result.stderr
    [argv] = runs
    assert argv[-1] == image
    assert "no-new-privileges" in argv


@pytest.mark.parametrize("verb", ["build", "start", "init", "rebuild"])
@pytest.mark.parametrize("image", ["-lodge", "lodge-b --privileged", "lodge;id", "$(id)", ".lodge", "/lodge"])
def test_a_bad_woltspace_image_stops_before_any_docker_call(tmp_path, verb, image):
    result, calls, _ = _docker_calls(tmp_path, verb, WOLTSPACE_IMAGE=image)
    assert result.returncode == 1
    assert "WOLTSPACE_IMAGE must be an image name" in result.stderr
    assert calls == []


# --------------------------------------------------------------------------
# The run-setting checks: a bad value stops `start` before the old container goes
# --------------------------------------------------------------------------

@pytest.mark.parametrize("env, message", [
    ({"WOLTSPACE_PUBLISH_HOST": "localhost"}, "WOLTSPACE_PUBLISH_HOST must be an IP address"),
    ({"WOLTSPACE_PUBLISH_HOST": "127.0.0.1 -v /:/host"}, "WOLTSPACE_PUBLISH_HOST must be an IP address"),
    ({"WOLTSPACE_PUBLISH_HOST": "-p"}, "WOLTSPACE_PUBLISH_HOST must be an IP address"),
    ({"WOLTSPACE_DOCKER_NETWORK": "host"}, "WOLTSPACE_DOCKER_NETWORK=host is not allowed"),
    ({"WOLTSPACE_DOCKER_NETWORK": "none"}, "WOLTSPACE_DOCKER_NETWORK=none is not allowed"),
    ({"WOLTSPACE_DOCKER_NETWORK": "--privileged"}, "WOLTSPACE_DOCKER_NETWORK must be a network name"),
    ({"WOLTSPACE_DOCKER_NETWORK": "lodge net"}, "WOLTSPACE_DOCKER_NETWORK must be a network name"),
    ({"WOLTSPACE_DOCKER_NETWORK": "net;id"}, "WOLTSPACE_DOCKER_NETWORK must be a network name"),
])
def test_a_rejected_run_setting_stops_start_before_rm_or_run(tmp_path, env, message):
    result, calls, _ = _docker_calls(tmp_path, **env)
    assert result.returncode == 1
    assert message in result.stderr
    assert not [c for c in calls if c[0] in ("rm", "run", "build")]


@pytest.mark.parametrize("host, published", [("127.0.0.1", "127.0.0.1:7777:7777"), ("::1", "[::1]:7777:7777"),
                                             ("fd00::2", "[fd00::2]:7777:7777")])
def test_the_port_is_published_on_the_host_address_ipv6_in_brackets(tmp_path, host, published):
    result, _, runs = _docker_calls(tmp_path, WOLTSPACE_PUBLISH_HOST=host)
    assert result.returncode == 0, result.stderr
    [argv] = runs
    assert argv[argv.index("-p") + 1] == published


def test_an_accepted_docker_network_is_joined(tmp_path):
    result, _, runs = _docker_calls(tmp_path, WOLTSPACE_DOCKER_NETWORK="lodge-b_egress.1")
    assert result.returncode == 0, result.stderr
    [argv] = runs
    assert argv[argv.index("--network") + 1] == "lodge-b_egress.1"


@pytest.mark.parametrize("external", [True, False])
def test_init_sees_an_existing_lodge_in_the_data_root_not_in_an_external_env_file(tmp_path, external):
    wolts_dir = tmp_path / "wolts"
    wolts_dir.mkdir()  # exists, and holds no lodge yet
    env_file = tmp_path / "lodge-b.env" if external else wolts_dir / ".env"
    env_file.write_text("WOLTSPACE_PUBLIC_TUNNEL=false\n")
    env = {"WOLTSPACE_ENV_FILE": str(env_file)} if external else {}
    result = _launch(wolts_dir, "init", WOLTSPACE_NONINTERACTIVE="true", WOLTSPACE_INIT_WOLT_NAME="first", **env)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = [c.split() for c in (tmp_path / "fake-bin" / "docker.log").read_text().splitlines()]
    [argv] = [c for c in calls if c[0] == "run"]
    assert argv[argv.index("--env-file") + 1] == str(env_file)
    # A pre-made external env file is not a lodge: init is a fresh one and names the first wolt.
    assert ("lodge exists" in result.stdout) is not external
    assert ("WOLTSPACE_WOLT_NAME=first" in argv) is external


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
