"""Resolve and launch the one exactly compatible TUI artifact."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from .compatibility import TUI_BINARY, TUI_PACKAGE, TUI_VERSION, tui_spec


class TuiResolutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class TuiResolution:
    source: str
    command: tuple[str, ...]
    local_probe: dict | None = None

    def to_record(self) -> dict:
        return {
            "source": self.source,
            "package": TUI_PACKAGE,
            "version": TUI_VERSION,
            "command": list(self.command),
            "local_probe": self.local_probe,
        }


def _probe(
    binary: str,
    *,
    runner: Callable = subprocess.run,
) -> dict:
    try:
        result = runner(
            [binary, "--version", "--json"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"path": binary, "valid": False, "error": str(exc)}
    try:
        payload = json.loads(result.stdout) if result.returncode == 0 else {}
    except (json.JSONDecodeError, TypeError):
        payload = {}
    valid = (
        payload.get("name") == TUI_PACKAGE
        and payload.get("version") == TUI_VERSION
        and payload.get("binary") == TUI_BINARY
    )
    record = {
        "path": binary,
        "valid": valid,
        "name": payload.get("name"),
        "version": payload.get("version"),
    }
    if not valid:
        record["error"] = (
            f"expected {tui_spec()}, got "
            f"{payload.get('name') or 'unknown'}@{payload.get('version') or 'unknown'}"
        )
    return record


def resolve_tui(
    env: Mapping[str, str] | None = None,
    *,
    which: Callable[[str], str | None] = shutil.which,
    runner: Callable = subprocess.run,
) -> TuiResolution:
    values = os.environ if env is None else env
    configured = values.get("WOLTSPACE_TUI_BIN", "").strip()
    local = configured or which(TUI_BINARY)
    probe = _probe(local, runner=runner) if local else None
    if probe and probe["valid"]:
        return TuiResolution("local", (str(Path(local)),), probe)

    node = which("node")
    node_major = 0
    if node:
        try:
            node_result = runner(
                [node, "--version"], capture_output=True, text=True,
                check=False, timeout=5,
            )
            match = re.match(r"^v?(\d+)", node_result.stdout.strip())
            node_major = int(match.group(1)) if match else 0
        except (OSError, subprocess.SubprocessError, ValueError):
            node_major = 0
    if node_major < 18:
        found = f" Found Node.js {node_major}." if node_major else ""
        raise TuiResolutionError(
            f"{tui_spec()} requires Node.js 18 or newer.{found} "
            "Install Node.js 18 or newer, then rerun `woltspace tui`."
        )

    npx = which("npx")
    if not npx:
        mismatch = f" Local candidate: {probe['error']}." if probe else ""
        raise TuiResolutionError(
            f"No exact {tui_spec()} TUI is installed and npx is unavailable."
            f"{mismatch} Install npm with npx, then rerun `woltspace tui`."
        )
    return TuiResolution(
        "npx",
        (npx, "--yes", f"--package={tui_spec()}", TUI_BINARY),
        probe,
    )


def launch_tui(resolution: TuiResolution, args: list[str]) -> None:
    command = [*resolution.command, *args]
    os.execvpe(command[0], command, dict(os.environ))
