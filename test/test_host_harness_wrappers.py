"""Native harness wrappers preserve host authentication and configuration."""

import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    "wrapper,binary",
    [("wclaude", "claude"), ("wcodex", "codex"), ("wopencode", "opencode")],
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
