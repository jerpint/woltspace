"""Native harness wrappers preserve host authentication and configuration."""

import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    "wrapper,binary",
    [
        ("wclaude", "claude"),
        ("wcodex", "codex"),
        ("wopencode", "opencode"),
        ("wpi", "pi"),
    ],
)
def test_host_wrapper_preserves_home_and_writes_no_wolt_credentials(
    wrapper, binary, tmp_path
):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    probe = fake_bin / binary
    probe.write_text(
        "#!/bin/sh\n"
        "printf 'HOME=%s\\n' \"$HOME\"\n"
        "printf 'CODEX_HOME=%s\\n' \"${CODEX_HOME:-}\"\n"
        "printf 'XDG_DATA_HOME=%s\\n' \"${XDG_DATA_HOME:-}\"\n"
    )
    probe.chmod(0o755)

    host_home = tmp_path / "host-home"
    host_home.mkdir()
    wolt_home = tmp_path / "wolt-home"
    wolt_home.mkdir()
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "HOME": str(host_home),
        "CODEX_HOME": str(host_home / "custom-codex"),
        "XDG_DATA_HOME": str(host_home / "custom-data"),
        "WOLTSPACE_ISOLATION": "host",
        "WOLTSPACE_WOLT_HOME": str(wolt_home),
    }
    result = subprocess.run(
        [str(ROOT / "container" / "bin" / wrapper)],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )

    assert f"HOME={host_home}" in result.stdout
    assert f"CODEX_HOME={host_home / 'custom-codex'}" in result.stdout
    assert f"XDG_DATA_HOME={host_home / 'custom-data'}" in result.stdout
    assert list(wolt_home.iterdir()) == []


def test_pi_external_wrapper_isolates_state_and_copies_seed(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    probe = fake_bin / "pi"
    probe.write_text(
        "#!/bin/sh\n"
        "printf 'HOME=%s\\n' \"$HOME\"\n"
        "printf 'PI_DIR=%s\\n' \"$PI_CODING_AGENT_DIR\"\n"
        "printf 'PI_SESSIONS=%s\\n' \"$PI_CODING_AGENT_SESSION_DIR\"\n"
        "printf 'ARGS=%s\\n' \"$*\"\n"
    )
    probe.chmod(0o755)

    wolt_home = tmp_path / "wolt-home"
    (wolt_home / ".claude" / "skills").mkdir(parents=True)
    (wolt_home / "CLAUDE.md").write_text("# Test wolt\n")
    shared = tmp_path / "shared-auth.json"
    shared.write_text('{"openrouter":"secret-marker"}\n')

    # The production seed path is fixed by the container mount. Replace that
    # one line in a private wrapper copy so this test never touches /workspace.
    wrapper = tmp_path / "wpi"
    source = (ROOT / "container" / "bin" / "wpi").read_text()
    source = source.replace(
        'SHARED_AUTH="/workspace/wolts/.pi/agent/auth.json"',
        f'SHARED_AUTH="{shared}"',
    )
    wrapper.write_text(source)
    wrapper.chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "WOLTSPACE_ISOLATION": "external",
        "WOLTSPACE_WOLT_HOME": str(wolt_home),
    }
    result = subprocess.run(
        [str(wrapper), "--version"], env=env, cwd=wolt_home,
        capture_output=True, text=True, check=True,
    )

    assert f"HOME={wolt_home}" in result.stdout
    assert f"PI_DIR={wolt_home / '.pi' / 'agent'}" in result.stdout
    assert f"PI_SESSIONS={wolt_home / '.pi' / 'agent' / 'sessions'}" in result.stdout
    assert "ARGS=--version" in result.stdout
    assert (wolt_home / ".pi" / "agent" / "auth.json").read_text() == shared.read_text()
    assert (wolt_home / "AGENTS.md").resolve() == (wolt_home / "CLAUDE.md")
    assert (wolt_home / ".agents" / "skills").resolve() == (wolt_home / ".claude" / "skills")
