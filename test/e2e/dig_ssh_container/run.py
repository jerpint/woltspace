#!/usr/bin/env python3
"""Installed-wheel Dig proof against a disposable loopback-only SSH target."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = Path(__file__).with_name("Dockerfile")


def run(argv, *, env=None, check=True, capture=True, input_text=None):
    return subprocess.run(
        [str(item) for item in argv],
        cwd=ROOT,
        env=env,
        check=check,
        capture_output=capture,
        text=True,
        input=input_text,
    )


def wait_for_port(container: str) -> tuple[str, int]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = run(["docker", "port", container, "22/tcp"], check=False)
        value = result.stdout.strip()
        if value:
            host, port = value.rsplit(":", 1)
            if host != "127.0.0.1":
                raise AssertionError(f"SSH was not loopback-only: {value}")
            return host, int(port)
        time.sleep(0.2)
    raise AssertionError("Docker never published the SSH port")


def scan_host_key(port: int) -> str:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = run(
            ["ssh-keyscan", "-T", "2", "-p", str(port), "127.0.0.1"],
            check=False,
        )
        lines = [line for line in result.stdout.splitlines() if line and not line.startswith("#")]
        if lines:
            return "\n".join(lines) + "\n"
        time.sleep(0.2)
    raise AssertionError("disposable SSH server never offered a host key")


def assert_private(path: Path, mode: int) -> None:
    actual = path.stat().st_mode & 0o777
    if actual != mode:
        raise AssertionError(f"{path} mode is {actual:o}, expected {mode:o}")


def main() -> int:
    for command in ("docker", "uv", "ssh", "ssh-keygen", "ssh-keyscan"):
        if shutil.which(command) is None:
            raise SystemExit(f"required command is missing: {command}")

    suffix = secrets.token_hex(5)
    image = f"woltspace-dig-e2e:{suffix}"
    container = f"woltspace-dig-e2e-{suffix}"

    with tempfile.TemporaryDirectory(prefix="woltspace-dig-e2e-") as raw_temp:
        temp = Path(raw_temp)
        context = temp / "context"
        artifacts = temp / "artifacts"
        client_home = temp / "client-home"
        client_wolts = temp / "client-wolts"
        venv = temp / "client-venv"
        for directory in (context, artifacts, client_home, client_wolts / "n00b"):
            directory.mkdir(parents=True)

        key = temp / "dig_key"
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", key])
        shutil.copyfile(f"{key}.pub", context / "authorized_keys")

        run(["uv", "build", "--wheel", "--out-dir", artifacts], capture=False)
        wheel = next(artifacts.glob("*.whl"))
        shutil.copyfile(wheel, context / wheel.name)

        try:
            run(["docker", "build", "-f", DOCKERFILE, "-t", image, context], capture=False)
            run([
                "docker", "run", "--rm", "-d", "--name", container,
                "--publish", "127.0.0.1::22", image,
            ])
            _, port = wait_for_port(container)

            known_hosts = temp / "known_hosts"
            known_hosts.write_text(scan_host_key(port))
            known_hosts.chmod(0o600)
            key.chmod(0o600)

            ssh_config = temp / "ssh_config"
            ssh_config.write_text(
                "Host dig-e2e\n"
                "  HostName 127.0.0.1\n"
                "  User colony\n"
                f"  Port {port}\n"
                f"  IdentityFile {key}\n"
                "  IdentitiesOnly yes\n"
                "  BatchMode yes\n"
                f"  UserKnownHostsFile {known_hosts}\n"
            )
            ssh_config.chmod(0o600)

            run(["uv", "venv", "--python", "3.13", venv], capture=False)
            run(["uv", "pip", "install", "--python", venv / "bin/python", wheel], capture=False)
            cli = venv / "bin/woltspace"
            env = {
                **os.environ,
                "HOME": str(client_home),
                "WOLTSPACE_WOLTS_DIR": str(client_wolts),
                "WOLTSPACE_WOLT_NAME": "n00b",
                "WOLTSPACE_DIG_SSH_CONFIG": str(ssh_config),
            }

            grant = run([
                cli, "dig", "grant", "throwaway", "dig-e2e",
                "--wolt", "n00b", "--json",
            ], env=env)
            grant_payload = json.loads(grant.stdout)
            resolved = grant_payload["grant"]["resolved"]
            assert resolved == {"hostname": "127.0.0.1", "user": "colony", "port": port}

            whoami = run([
                cli, "dig", "connect", "--json", "throwaway", "--", "whoami",
            ], env=env)
            assert whoami.stdout.strip() == "colony"

            remote_script = (
                "from pathlib import Path; import json; "
                "root=Path.home()/'.woltspace'; "
                "w=root/'wolts'/'seedling'/'wolt'; w.mkdir(parents=True, exist_ok=True); "
                "(w/'wolt.json').write_text(json.dumps({'name':'seedling','type':'raccoon'})+'\\n'); "
                "b=root/'bootstrap'; b.mkdir(parents=True, exist_ok=True); "
                "(b/'dig-handoff.json').write_text(json.dumps({"
                "'version':'woltspace.dig-handoff/v0','visitor':'n00b',"
                "'created_colony':'seedling','status':'ready'},sort_keys=True)+'\\n')"
            )
            run([
                cli, "dig", "connect", "--json", "throwaway", "--",
                "python", "-c", remote_script,
            ], env=env)

            handoff = run([
                cli, "dig", "connect", "--json", "throwaway", "--",
                "cat", ".woltspace/bootstrap/dig-handoff.json",
            ], env=env)
            handoff_payload = json.loads(handoff.stdout)
            assert handoff_payload == {
                "created_colony": "seedling",
                "status": "ready",
                "version": "woltspace.dig-handoff/v0",
                "visitor": "n00b",
            }

            remote_version = run([
                cli, "dig", "connect", "--json", "throwaway", "--",
                "python", "-c", "import woltspace; print(woltspace.__version__)",
            ], env=env)
            assert remote_version.stdout.strip() == "0.5.6"

            listing = json.loads(run([cli, "dig", "list", "--json"], env=env).stdout)
            audit = listing["grants"][0]
            assert audit["connect_count"] == 4
            assert audit["last_exit_code"] == 0
            store = client_wolts / ".space" / "digs" / "grants.json"
            assert_private(store.parent, 0o700)
            assert_private(store, 0o600)
            assert "PRIVATE KEY" not in store.read_text()
            assert remote_script not in store.read_text()

            revoked = json.loads(run([
                cli, "dig", "revoke", "throwaway", "--json",
            ], env=env).stdout)
            assert revoked == {"ok": True, "revoked": True, "name": "throwaway"}
            refused = run([
                cli, "dig", "connect", "--json", "throwaway", "--", "true",
            ], env=env, check=False)
            assert refused.returncode == 1
            assert json.loads(refused.stdout)["error"] == "unknown dig: throwaway"

            print(json.dumps({
                "ok": True,
                "transport": f"127.0.0.1:{port}",
                "remote_user": "colony",
                "installed_woltspace": remote_version.stdout.strip(),
                "handoff": handoff_payload,
                "connections_audited": audit["connect_count"],
                "revoked": True,
            }, indent=2))
        finally:
            run(["docker", "rm", "-f", container], check=False)
            run(["docker", "image", "rm", "-f", image], check=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
