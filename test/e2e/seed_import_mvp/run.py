#!/usr/bin/env python3
"""Disposable installed-wheel proof for the seed import MVP."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path


def run(*args: str, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(args, capture_output=True, text=True, env=env)
    if check and result.returncode:
        raise RuntimeError(f"command failed: {args}\n{result.stdout}\n{result.stderr}")
    return result


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_json(url: str, timeout: float = 15) -> object:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                return json.loads(response.read())
        except Exception:
            time.sleep(0.1)
    raise RuntimeError(f"timed out waiting for {url}")


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: run.py /path/to/woltspace.whl")
    wheel = Path(sys.argv[1]).resolve()
    if not wheel.is_file():
        raise SystemExit(f"wheel not found: {wheel}")

    root = Path(tempfile.mkdtemp(prefix="woltspace-seed-import-e2e-"))
    server: subprocess.Popen | None = None
    try:
        venv = root / "venv"
        run("uv", "venv", "--python", "3.13", str(venv))
        python = venv / "bin" / "python"
        run("uv", "pip", "install", "--python", str(python), str(wheel))
        cli = venv / "bin" / "woltspace"

        source_wolts = root / "source-wolts"
        keeper = source_wolts / "keeper"
        write_json(keeper / "wolt/wolt.json", {
            "name": "keeper", "type": "raccoon", "role": "Keeps a tiny app",
        })
        (keeper / "wolt/memory").mkdir(parents=True)
        (keeper / "wolt/memory/identity.md").write_text("# keeper\n\nA portable keeper.\n")
        (keeper / "CLAUDE.md").write_text(
            "<!-- WOLTSPACE:BEGIN — auto-managed, do not edit -->\nold platform\n"
            "<!-- WOLTSPACE:END -->\n\n# keeper\n\nKeep the tiny app healthy.\n"
        )
        skill = keeper / ".claude/skills/tiny-care"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# Tiny Care\n\nInspect the tiny app.\n")
        script = skill / "check.sh"
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o755)

        other = source_wolts / "other"
        write_json(other / "wolt/wolt.json", {"name": "other", "type": "beaver"})
        (other / "wolt/memory").mkdir(parents=True)
        (other / "wolt/memory/identity.md").write_text("# other\n")
        (other / "CLAUDE.md").write_text(
            "<!-- WOLTSPACE:BEGIN — auto-managed, do not edit -->\nplatform\n"
            "<!-- WOLTSPACE:END -->\n\n# other\n"
        )

        app = source_wolts / "apps/tiny-app"
        app.mkdir(parents=True)
        write_json(app / "woltspace.json", {
            "name": "tiny-app", "description": "Tiny", "stack": "static",
            "start": "python -m http.server", "port": 4555,
            "keeper": "keeper", "public": True, "source": None,
        })
        (app / "index.html").write_text("tiny\n")
        run("git", "init", "-q", str(app))
        run("git", "-C", str(app), "add", ".")
        run("git", "-C", str(app), "-c", "user.name=Test", "-c",
            "user.email=test@example.invalid", "commit", "-qm", "app")

        package = root / "seed-repo"
        source_env = {**os.environ, "WOLTSPACE_WOLTS_DIR": str(source_wolts)}
        run(
            str(cli), "seed", "create", str(package), "--name", "starter-team",
            "--wolt", "keeper", "--wolt", "other", "--app", "tiny-app",
            "--skill", "keeper:tiny-care", env=source_env,
        )
        run("git", "init", "-q", str(package))
        run("git", "-C", str(package), "add", ".")
        run("git", "-C", str(package), "-c", "user.name=Test", "-c",
            "user.email=test@example.invalid", "commit", "-qm", "seed v0.1.0")
        run("git", "-C", str(package), "tag", "v0.1.0")
        (package / "wolts/keeper/rules.md").write_text("dirty worktree must not import\n")

        inspected = json.loads(run(
            str(cli), "seed", "inspect", str(package), "--ref", "v0.1.0", "--json",
        ).stdout)
        assert inspected["ok"] is True
        keeper_preview = next(c for c in inspected["components"] if c["id"] == "wolt:keeper")
        assert "Keep the tiny app healthy" in keeper_preview["rules"]
        assert "dirty worktree" not in keeper_preview["rules"]
        assert any(item["executable"] for item in keeper_preview["skill_files"])

        lodge = root / "lodge"
        lodge.mkdir()
        port = free_port()
        lodge_env = {
            **os.environ,
            "WOLTSPACE_WOLTS_DIR": str(lodge),
            "WOLTSPACE_HOST": "127.0.0.1",
            "WOLTSPACE_PORT": str(port),
        }
        server = subprocess.Popen(
            [str(cli), "serve", "--host", "127.0.0.1", "--port", str(port),
             "--no-doctor", "--log-level", "warning"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=lodge_env,
        )
        assert wait_json(f"http://127.0.0.1:{port}/wolts") == []

        imported = json.loads(run(
            str(cli), "seed", "import", str(package), "--ref", "v0.1.0",
            "--app", "tiny-app", "--json", env=lodge_env,
        ).stdout)
        assert imported["wolts"] == ["keeper"]
        assert imported["apps"] == ["tiny-app"]
        assert imported["required"] == ["keeper"]
        assert not (lodge / "other").exists()
        visible = wait_json(f"http://127.0.0.1:{port}/wolts")
        assert [item["name"] for item in visible] == ["keeper"]

        config = json.loads((lodge / "keeper/wolt/wolt.json").read_text())
        assert config["seed"]["install_id"] == imported["install_id"]
        assert config["seed"]["component_id"] == "wolt:keeper"
        assert config["seed"]["resolved_commit"] == inspected["source"]["resolved_commit"]
        receipt = Path(imported["receipt"])
        assert receipt.is_file()
        assert (receipt.parent / "base/wolts/keeper/rules.md").is_file()
        assert json.loads((lodge / "apps/tiny-app/woltspace.json").read_text())["public"] is False

        shutil.rmtree(package)
        local_status = json.loads(run(
            str(cli), "seed", "status", "--wolt", "keeper", "--json", env=lodge_env,
        ).stdout)
        assert local_status["ok"] is True
        assert local_status["changes"] == []

        print(json.dumps({
            "ok": True,
            "wheel": wheel.name,
            "install_id": imported["install_id"],
            "visible_wolts": [item["name"] for item in visible],
            "selected_apps": imported["apps"],
            "source_removed_status": "pass",
        }, indent=2))
        return 0
    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

