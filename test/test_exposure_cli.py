"""Host CLI tests for exposure management without a Docker daemon."""

import os
import stat
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "woltspace"


def _cli(tmp_path: Path, *args: str, env_text: str = "") -> subprocess.CompletedProcess:
    wolts = tmp_path / "wolts"
    (wolts / "testwolt" / "wolt").mkdir(parents=True)
    (wolts / ".env").write_text(env_text)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text("#!/bin/sh\nexit 0\n")
    docker.chmod(0o755)

    env = {
        **os.environ,
        "WOLTS_DIR": str(wolts),
        "WOLTSPACE_CONTAINER": "woltspace-exposure-test",
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }
    return subprocess.run(
        [str(CLI), "exposure", *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_status_defaults_to_off(tmp_path):
    result = _cli(tmp_path, "status")
    assert result.returncode == 0
    assert "exposure: off" in result.stdout
    assert "127.0.0.1" in result.stdout


def test_temporary_updates_canonical_setting(tmp_path):
    result = _cli(tmp_path, "temporary", env_text="WOLTSPACE_PUBLIC_TUNNEL=false\n")
    assert result.returncode == 0
    env_file = tmp_path / "wolts" / ".env"
    assert "WOLTSPACE_EXPOSURE=temporary" in env_file.read_text()
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_authenticated_fails_closed_without_named_tunnel_config(tmp_path):
    result = _cli(tmp_path, "authenticated")
    assert result.returncode == 1
    assert "requires CLOUDFLARE_TUNNEL_TOKEN" in result.stdout
    assert "WOLTSPACE_EXPOSURE=authenticated" not in (
        tmp_path / "wolts" / ".env"
    ).read_text()


def test_authenticated_accepts_existing_named_tunnel_config(tmp_path):
    result = _cli(
        tmp_path,
        "authenticated",
        env_text=(
            "CLOUDFLARE_TUNNEL_TOKEN=synthetic-token\n"
            "CLOUDFLARE_TUNNEL_URL=https://example.invalid\n"
        ),
    )
    assert result.returncode == 0
    assert "WOLTSPACE_EXPOSURE=authenticated" in (
        tmp_path / "wolts" / ".env"
    ).read_text()
