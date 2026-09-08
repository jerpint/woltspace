"""`container/bin` scripts must run on a native install, and fail safely.

Two problems, one family. `container/bin/gh-app-token` asks for
`#!/usr/bin/env woltspace-python`, a name the *image* creates as a symlink and a
native host has never had — so on a native colony it died with
`env: woltspace-python: No such file or directory`. And every caller spells it

    GH_TOKEN=$(gh-app-token) gh ...

which means a token command that fails hands `gh` an empty GH_TOKEN, and `gh`
then falls back to the human's own stored credentials. A failure that prints an
*error string* to stdout is worse still: it satisfies a caller's
"token is non-empty" check and gets used as a credential.

So: the name resolves natively now (a shim in the same directory), and the token
script's stdout is a validated `ghs_` token or it is empty.

Isolation contract for this file — every probe runs with:
  * the data-root variable pointed at a tmp_path, set *explicitly* and under
    both its canonical and legacy spellings. Merely unsetting it makes
    the real CLI default to the live data root, which is how an earlier test
    file reached a running colony.
  * WOLTSPACE_API pointed at a closed port.
  * PATH built from scratch. Never inherited, so no real `woltspace` and no real
    `gh` is reachable.
  * no lifecycle verbs, and no network: credentials are deliberately bogus so
    every path fails before reaching api.github.com.
The `sandbox_is_not_the_live_colony` fixture asserts all of that and fails the
run rather than letting a probe escape.
"""

import importlib.machinery
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "container" / "bin"
SHIM = BIN / "woltspace-python"
TOKEN_SCRIPT = BIN / "gh-app-token"

# The exact probe the shim uses to decide an interpreter owns what the scripts
# behind it import. `woltspace.envvars` is part of it: gh-app-token imports it
# at module level, so a python with PyJWT and python-dotenv but no woltspace
# package produces an ImportError and an empty stdout — a token that wasn't.
DEPS_PROBE = "import jwt, dotenv, woltspace.envvars"

# A port nothing listens on. Not 7777 — that is the colony's, and the point of
# this constant is that a probe which somehow builds a real client fails to
# connect instead of succeeding against the live control plane.
DEAD_PORT = 9
DEAD_API = f"http://127.0.0.1:{DEAD_PORT}"
SYSTEM_PATH = f"/usr/bin{os.pathsep}/bin"
LIVE_ENDPOINTS = {"127.0.0.1:7777", "localhost:7777", "[::1]:7777"}


def sandbox_env(tmp_path, *, extra_path="", **extra):
    """The only environment a probe in this file is allowed to run in."""
    wolts_dir = tmp_path / "wolts"
    wolts_dir.mkdir(exist_ok=True)
    path = f"{extra_path}{os.pathsep}{SYSTEM_PATH}" if extra_path else SYSTEM_PATH
    env = {
        "HOME": str(tmp_path / "home"),
        "PATH": path,
        # Explicit, not absent. An absent data-root variable means "the live
        # one" — the default is the running colony, so unsetting is not
        # isolation.
        #
        # Both spellings, canonical first: the scripts under test resolve the
        # canonical name and fall back to the legacy one, and a probe that set
        # only the old name would be testing the fallback rather than the
        # behaviour. Reads below take the canonical key.
        "WOLTSPACE_WOLTS_DIR": str(wolts_dir),
        "WOLTS_DIR": str(wolts_dir),
        "WOLTSPACE_WOLT_DIR": str(wolts_dir),
        "WOLT_DIR": str(wolts_dir),
        "WOLTSPACE_API": DEAD_API,
        "WOLTSPACE_ISOLATION": "host",
    }
    Path(env["HOME"]).mkdir(exist_ok=True)
    env.update(extra)
    return env


@pytest.fixture(autouse=True)
def sandbox_is_not_the_live_colony(tmp_path):
    """Tripwire. Fails the run if a probe could reach the real colony."""
    env = sandbox_env(tmp_path)

    api = env["WOLTSPACE_API"]
    assert not any(live in api for live in LIVE_ENDPOINTS), (
        f"probe API {api} points at the live control plane"
    )

    wolts_dir = Path(env["WOLTSPACE_WOLTS_DIR"])
    assert tmp_path in wolts_dir.parents or wolts_dir == tmp_path / "wolts", (
        f"data root {wolts_dir} is outside the test's tmp_path"
    )
    live_root = Path("~/.woltspace/wolts").expanduser()
    assert wolts_dir != live_root, f"sandbox points at the live data root {live_root}"

    for entry in env["PATH"].split(os.pathsep):
        for name in ("woltspace", "gh"):
            assert not (Path(entry) / name).exists(), (
                f"sandbox PATH entry {entry} exposes a real {name}"
            )
    yield


def run_shim(argv, env):
    return subprocess.run(
        ["sh", str(SHIM), *argv],
        capture_output=True, text=True, timeout=30, env=env,
    )


# --------------------------------------------------------------------------
# The interpreter shim
# --------------------------------------------------------------------------

def _noisy_interpreter(directory, name, marker, noise):
    """A python that greets stdout — a banner, a telemetry notice, a plugin.

    It still answers the dependency probe correctly. The point is that the probe
    must not let the greeting through: everything behind this shim promises a
    machine-readable stdout.
    """
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / name
    script.write_text(
        "#!/bin/sh\n"
        f"echo '{noise}'\n"
        f'if [ "$1" = "-c" ] && [ "$2" = "{DEPS_PROBE}" ]; then\n'
        "  exit 0\n"
        "fi\n"
        f"echo '{marker}'\n"
        'echo "args=$*"\n'
    )
    script.chmod(0o755)
    return script


def test_the_dependency_probe_swallows_stdout_noise(tmp_path):
    """A probe that leaks the banner would break every caller's stdout contract.

    Before this, `_owns_deps` redirected only stderr. A python printing to
    stdout got its banner emitted by the *probe*, ahead of the real payload —
    so `GH_TOKEN=$(gh-app-token)` could capture "Telemetry enabled\\nghs_…".
    It failed safe only by accident: the noise broke the ghs_ prefix match.
    """
    venv = tmp_path / "venv"
    bin_dir = (venv / "lib" / "python3.13" / "site-packages" / "woltspace"
               / "_bundle" / "container" / "bin")
    bin_dir.mkdir(parents=True)
    shim = bin_dir / "woltspace-python"
    shim.write_text(SHIM.read_text())
    shim.chmod(0o755)
    _noisy_interpreter(
        venv / "bin", "python", "PAYLOAD",
        noise="Telemetry is enabled. See https://example.invalid",
    )

    result = subprocess.run(
        ["sh", str(shim), "-V"],
        capture_output=True, text=True, timeout=30, env=sandbox_env(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    # The chosen interpreter still runs, and its own output is untouched...
    assert "PAYLOAD" in result.stdout
    # ...but the probe contributed nothing. Exactly one banner line — the
    # interpreter's own, from the real invocation — not two.
    assert result.stdout.count("Telemetry is enabled") == 1, (
        f"probe leaked stdout noise:\n{result.stdout}"
    )
    # And the payload is not preceded by a second copy of the greeting.
    assert result.stdout.splitlines()[0].startswith("Telemetry")
    assert result.stdout.splitlines()[1] == "PAYLOAD"


def _fake_interpreter(directory, name, marker, *, owns_deps=True):
    """A stand-in python that identifies itself and echoes its arguments.

    The shim probes a candidate with `-c 'import jwt, dotenv, woltspace.envvars'`
    before trusting it, so a usable fake has to answer that probe.
    `owns_deps=False` models the interpreter that exists but owns none of the
    wheel's dependencies — an ambient `python3`.
    """
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / name
    probe_exit = 0 if owns_deps else 1
    script.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "-c" ] && [ "$2" = "{DEPS_PROBE}" ]; then\n'
        f"  exit {probe_exit}\n"
        "fi\n"
        f"echo '{marker}'\n"
        'echo "args=$*"\n'
    )
    script.chmod(0o755)
    return script


def test_explicit_override_wins(tmp_path):
    chosen = _fake_interpreter(tmp_path / "chosen", "python", "OVERRIDE")
    result = run_shim(
        ["-c", "pass"],
        sandbox_env(tmp_path, WOLTSPACE_PYTHON=str(chosen)),
    )
    assert result.returncode == 0, result.stderr
    assert "OVERRIDE" in result.stdout
    assert "args=-c pass" in result.stdout


def test_resolves_the_venv_python_beside_its_own_bundle(tmp_path):
    """The native answer, and it is arithmetic rather than a PATH search.

    A copy of the shim placed at the real bundle depth must find
    `<venv>/bin/python` with no environment help at all.
    """
    venv = tmp_path / "venv"
    bin_dir = (venv / "lib" / "python3.13" / "site-packages" / "woltspace"
               / "_bundle" / "container" / "bin")
    bin_dir.mkdir(parents=True)
    shim = bin_dir / "woltspace-python"
    shim.write_text(SHIM.read_text())
    shim.chmod(0o755)
    _fake_interpreter(venv / "bin", "python", "VENV PYTHON")

    result = subprocess.run(
        ["sh", str(shim), "-m", "somemodule"],
        capture_output=True, text=True, timeout=30, env=sandbox_env(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    assert "VENV PYTHON" in result.stdout
    assert "args=-m somemodule" in result.stdout


def test_borrows_the_interpreter_from_the_console_script_shebang(tmp_path):
    """Covers non-venv shapes: pip --user, or a bundle run from a checkout."""
    interpreter = _fake_interpreter(tmp_path / "interp", "python3.13", "SHEBANG PYTHON")
    cli_dir = tmp_path / "cli-bin"
    cli_dir.mkdir()
    console = cli_dir / "woltspace"
    console.write_text(f"#!{interpreter}\nraise SystemExit\n")
    console.chmod(0o755)

    env = sandbox_env(tmp_path)
    # The tripwire forbids a real `woltspace` on PATH; this one is a fake whose
    # only job is to carry a shebang, so add it after the fixture has run.
    env["PATH"] = f"{cli_dir}{os.pathsep}{env['PATH']}"
    result = run_shim(["-V"], env)
    assert result.returncode == 0, result.stderr
    assert "SHEBANG PYTHON" in result.stdout


def test_never_borrows_the_thin_client_shebang(tmp_path):
    """The regression that a real end-to-end run caught.

    `container/bin` holds a `woltspace` of its own — the thin HTTP control
    client, shebanged `#!/usr/bin/env python3`. Borrowing from it resolves the
    *ambient* python, which owns no PyJWT, and `gh-app-token` then failed with
    "PyJWT is missing" on a host where a perfectly good interpreter existed.
    """
    bundle_bin = tmp_path / "checkout" / "container" / "bin"
    bundle_bin.mkdir(parents=True)
    thin_client = bundle_bin / "woltspace"
    thin_client.write_text("#!/usr/bin/env python3\nraise SystemExit\n")
    thin_client.chmod(0o755)
    # An ambient python3 that exists but owns nothing.
    ambient = tmp_path / "ambient"
    _fake_interpreter(ambient, "python3", "AMBIENT PYTHON", owns_deps=False)
    # And the real one, reachable as a console script elsewhere.
    good = _fake_interpreter(tmp_path / "good", "python3.13", "REAL PYTHON")
    console_dir = tmp_path / "console"
    console_dir.mkdir()
    console = console_dir / "woltspace"
    console.write_text(f"#!{good}\nraise SystemExit\n")
    console.chmod(0o755)

    env = sandbox_env(tmp_path)
    env["PATH"] = os.pathsep.join(
        [str(bundle_bin), str(ambient), str(console_dir), env["PATH"]]
    )
    result = run_shim(["-V"], env)
    assert result.returncode == 0, result.stderr
    assert "REAL PYTHON" in result.stdout
    assert "AMBIENT PYTHON" not in result.stdout


def test_a_candidate_without_the_dependencies_is_rejected(tmp_path):
    """Verified, not assumed — including the venv-shaped answer."""
    venv = tmp_path / "venv"
    bin_dir = (venv / "lib" / "python3.13" / "site-packages" / "woltspace"
               / "_bundle" / "container" / "bin")
    bin_dir.mkdir(parents=True)
    shim = bin_dir / "woltspace-python"
    shim.write_text(SHIM.read_text())
    shim.chmod(0o755)
    _fake_interpreter(venv / "bin", "python", "EMPTY VENV", owns_deps=False)

    result = subprocess.run(
        ["sh", str(shim), "-V"],
        capture_output=True, text=True, timeout=30, env=sandbox_env(tmp_path),
    )
    assert result.returncode == 1
    assert "EMPTY VENV" not in result.stdout
    assert "no interpreter found" in result.stderr


def _load_token_script():
    """Import `gh-app-token` as a module — it has no .py suffix and no side
    effects at import (everything is behind `if __name__ == "__main__"`)."""
    import importlib.util

    spec = importlib.util.spec_from_loader(
        "gh_app_token_under_test",
        importlib.machinery.SourceFileLoader(
            "gh_app_token_under_test", str(TOKEN_SCRIPT)),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_an_explicit_data_root_does_not_borrow_the_default_colony_env(
    tmp_path, monkeypatch
):
    """A colony at its own data root must not mint the *default* colony's app.

    The home default is a fallback for shells that carry no data root at all.
    Appending it unconditionally meant a colony whose own `.env` has no GitHub
    App silently picked up `~/.woltspace/wolts/.env` and minted a real token
    for somebody else's app — a wrong identity the caller cannot detect,
    because the token is perfectly well-formed.
    """
    module = _load_token_script()
    home = tmp_path / "home"
    (home / ".woltspace" / "wolts").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("WOLTSPACE_WOLT_DIR", raising=False)
    monkeypatch.delenv("WOLT_DIR", raising=False)

    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(tmp_path / "colony"))
    files = [str(path) for path in module._env_files()]
    assert str(tmp_path / "colony" / ".env") in files
    assert str(home / ".woltspace" / "wolts" / ".env") not in files

    # With no data root named, the default is still the right guess.
    monkeypatch.delenv("WOLTSPACE_WOLTS_DIR")
    monkeypatch.delenv("WOLTS_DIR", raising=False)
    files = [str(path) for path in module._env_files()]
    assert str(home / ".woltspace" / "wolts" / ".env") in files


def test_pyjwt_alone_is_not_enough(tmp_path):
    """The gap #405 left open: `gh-app-token` needs the wheel, not just PyJWT.

    An interpreter that imports jwt and dotenv but has no `woltspace` package
    dies on gh-app-token's module-level `from woltspace.envvars import get_env`
    — traceback on stderr, nothing on stdout, and `GH_TOKEN=$(gh-app-token) gh
    …` then acts as the human. So the probe asks for all three.
    """
    half = tmp_path / "half"
    half.mkdir()
    script = half / "python3"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-c" ] && [ "$2" = "import jwt, dotenv" ]; then\n'
        "  exit 0\n"          # owns the libraries...
        "fi\n"
        'if [ "$1" = "-c" ]; then\n'
        "  exit 1\n"          # ...but not the woltspace package
        "fi\n"
        "echo 'HALF PYTHON'\n"
    )
    script.chmod(0o755)

    result = run_shim(["-V"], sandbox_env(tmp_path, extra_path=str(half)))

    assert result.returncode == 1
    assert "HALF PYTHON" not in result.stdout
    assert "no interpreter found" in result.stderr


def test_fails_loudly_when_no_interpreter_owns_the_dependencies(tmp_path):
    result = run_shim(["-c", "pass"], sandbox_env(tmp_path))
    assert result.returncode == 1
    assert result.stdout == ""
    assert "no interpreter found" in result.stderr
    assert "connectors" in result.stderr
    assert "WOLTSPACE_PYTHON" in result.stderr


def test_shim_never_execs_itself(tmp_path):
    """A `woltspace-python` first on PATH must not become its own interpreter.

    The shim shadows /usr/local/bin/woltspace-python in the container, so a
    self-exec here would be an infinite loop at container boot.
    """
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    copy = shadow / "woltspace-python"
    copy.write_text(SHIM.read_text())
    copy.chmod(0o755)

    env = sandbox_env(tmp_path, extra_path=str(shadow))
    result = subprocess.run(
        ["sh", str(copy), "-c", "pass"],
        capture_output=True, text=True, timeout=15, env=env,
    )
    # Terminates with the honest failure rather than spinning.
    assert result.returncode == 1
    assert "no interpreter found" in result.stderr


def test_every_bundled_shebang_can_now_be_satisfied():
    """No `container/bin` script may name an interpreter the bundle lacks."""
    shipped = {entry.name for entry in BIN.iterdir() if entry.is_file()}
    for entry in sorted(BIN.iterdir()):
        if not entry.is_file():
            continue
        shebang = entry.read_text(errors="replace").splitlines()[:1]
        if not shebang or not shebang[0].startswith("#!"):
            continue
        line = shebang[0]
        if "/usr/bin/env " not in line:
            continue
        interpreter = line.split("/usr/bin/env ", 1)[1].split()[0]
        if interpreter in {"python3", "bash", "sh", "node"}:
            continue
        assert interpreter in shipped, (
            f"{entry.name} wants `{interpreter}`, which the bundle does not "
            f"ship — it will die with `env: {interpreter}: not found` natively"
        )


# --------------------------------------------------------------------------
# gh-app-token: stdout is a token or it is empty
# --------------------------------------------------------------------------

def run_token_script(env):
    """Run the token script under this interpreter, bypassing the shebang.

    No network is reachable from any of these cases: the credentials are bogus,
    so every path exits before the api.github.com call.
    """
    return subprocess.run(
        [sys.executable, str(TOKEN_SCRIPT)],
        capture_output=True, text=True, timeout=30, env=env,
    )


def test_missing_credentials_produce_empty_stdout_and_nonzero_exit(tmp_path):
    """The failure that let a PR be opened under a human's name."""
    result = run_token_script(sandbox_env(tmp_path))
    assert result.returncode != 0
    assert result.stdout == "", "stdout must stay empty so $(...) captures nothing"
    assert "missing env vars" in result.stderr
    # And it says where it looked, so the fix is obvious.
    assert "GITHUB_APP_ID" in result.stderr


def test_error_text_never_reaches_stdout_even_merged(tmp_path):
    """`$(gh-app-token 2>&1)` must not yield a plausible-looking token.

    This is the exact shape of the mistake: an error string long enough to pass
    a `[ -n "$T" ]` / length check. It can still be captured when a caller
    merges the streams, so the contract callers must hold is the `ghs_` prefix.
    """
    result = subprocess.run(
        [sys.executable, str(TOKEN_SCRIPT)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, timeout=30, env=sandbox_env(tmp_path),
    )
    assert result.returncode != 0
    assert not result.stdout.startswith("ghs_")


def test_a_non_pem_key_fails_before_any_network_call(tmp_path):
    env = sandbox_env(
        tmp_path,
        GITHUB_APP_ID="12345",
        GITHUB_APP_INSTALLATION_ID="67890",
        GITHUB_APP_PRIVATE_KEY="definitely-not-a-key",
    )
    result = run_token_script(env)
    assert result.returncode != 0
    assert result.stdout == ""
    assert "PEM key" in result.stderr


def test_credentials_are_read_from_the_data_root_env_file(tmp_path):
    """Native colonies keep `.env` in the data root, not `/workspace/wolts`."""
    env = sandbox_env(tmp_path)
    env_file = Path(env["WOLTSPACE_WOLTS_DIR"]) / ".env"
    env_file.write_text(
        "GITHUB_APP_ID=12345\n"
        "GITHUB_APP_INSTALLATION_ID=67890\n"
        "GITHUB_APP_PRIVATE_KEY=not-a-pem-either\n"
    )
    result = run_token_script(env)
    assert result.returncode != 0
    assert result.stdout == ""
    # It got past the "missing" check, so the .env was read — and stopped at the
    # PEM gate, before any network call.
    assert "missing env vars" not in result.stderr
    assert "PEM key" in result.stderr


def test_the_searched_paths_are_reported_and_exclude_nothing_obvious(tmp_path):
    result = run_token_script(sandbox_env(tmp_path))
    assert str(Path(sandbox_env(tmp_path)["WOLTSPACE_WOLTS_DIR"]) / ".env") in result.stderr
    assert "/workspace/wolts/.env" in result.stderr


def test_only_installation_tokens_are_printed():
    """A response that is not a ghs_ token is described, never echoed."""
    source = TOKEN_SCRIPT.read_text()
    assert 'TOKEN_PREFIX = "ghs_"' in source
    assert "token.startswith(TOKEN_PREFIX)" in source
    # The rejection path must not interpolate the value itself.
    rejection = source.split("token.startswith(TOKEN_PREFIX)", 1)[1][:600]
    assert "{token}" not in rejection


def test_the_bot_asserts_the_token_shape_before_using_it():
    """`open_issue` is the in-process caller and must hold the same contract.

    Checked at the source level rather than by import: `core.py` pulls litellm
    and the whole bot stack, which is far more than this seam is worth.
    """
    source = (ROOT / "container" / "bot" / "core.py").read_text()
    mint = source.split("gh-app-token", 1)[1][:1200]
    assert 'startswith("ghs_")' in mint, (
        "open_issue must reject anything that is not an installation token"
    )


def test_every_diagnostic_goes_to_stderr():
    """No `print(...)` in the script may default to stdout except the token."""
    source = TOKEN_SCRIPT.read_text()
    prints = [
        line.strip() for line in source.splitlines()
        if line.strip().startswith("print(")
    ]
    for line in prints:
        assert "file=sys.stderr" in line or line == "print(get_token())", (
            f"stray stdout write: {line}"
        )
