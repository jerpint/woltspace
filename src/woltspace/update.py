"""Safe, uv-managed updates for native Woltspace installations."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from packaging.version import InvalidVersion, Version

from . import __version__
from .layout import RuntimeLayout


PYPI_URL = "https://pypi.org/pypi/woltspace/json"
RELEASE_URL = "https://github.com/jerpint/woltspace/releases/tag/v{version}"


class UpdateError(RuntimeError):
    """An update cannot safely continue."""


@dataclass(frozen=True)
class Target:
    version: Version
    prerelease: bool


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def _uv_manages_exact_registry_install(installed: Version) -> None:
    result = _run(["uv", "tool", "list", "--show-version-specifiers"])
    if result.returncode:
        raise UpdateError("cannot read the uv tool receipt; update this installation with its original installer")
    line = next(
        (line.strip() for line in result.stdout.splitlines() if re.match(r"^woltspace\s+v?\S+", line.strip())),
        "",
    )
    if not line:
        raise UpdateError("Woltspace is not a uv-managed tool install")
    # uv currently renders the original requirement after the installed version.
    # Only a plain registry requirement is safe to replace with our exact pin.
    lower = line.lower()
    if any(marker in lower for marker in (" @ ", "git+", "file:", "path:", "editable")):
        raise UpdateError("the uv receipt uses a custom source; update it with its original recipe")
    specs = re.findall(r"woltspace(?:\[[^]]+\])?\s*(?:==\s*[^\s,;\]]+)?", line, re.I)
    if not specs:
        raise UpdateError("the uv receipt does not contain a safe registry recipe")
    if "[" in specs[-1]:
        raise UpdateError("the uv receipt contains extras; update it with its original recipe")
    match = re.search(r"==\s*([^\s,;\]]+)", specs[-1])
    if match:
        try:
            if Version(match.group(1)) != installed:
                raise UpdateError("the uv receipt pin does not match the installed version")
        except InvalidVersion as exc:
            raise UpdateError("the uv receipt contains an invalid version pin") from exc


def _pypi() -> dict[str, Any]:
    try:
        with urllib.request.urlopen(PYPI_URL, timeout=15) as response:
            return json.loads(response.read().decode())
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise UpdateError(f"cannot read PyPI release metadata: {exc}") from exc


def _usable_release(files: Any) -> bool:
    return bool(files) and not all(bool(item.get("yanked")) for item in files)


def _target(metadata: dict[str, Any], requested: str, allow_pre: bool) -> Target:
    releases = metadata.get("releases") or {}
    if requested:
        try:
            version = Version(requested)
        except InvalidVersion as exc:
            raise UpdateError(f"invalid target version: {requested}") from exc
        files = releases.get(str(version))
        if not files:
            raise UpdateError(f"Woltspace {version} is not published on PyPI")
        if not _usable_release(files):
            raise UpdateError(f"Woltspace {version} is yanked on PyPI")
        return Target(version, version.is_prerelease)

    candidates: list[Version] = []
    for raw, files in releases.items():
        try:
            version = Version(raw)
        except InvalidVersion:
            continue
        if (allow_pre or not version.is_prerelease) and _usable_release(files):
            candidates.append(version)
    if not candidates:
        raise UpdateError("PyPI has no eligible Woltspace release")
    version = max(candidates)
    return Target(version, version.is_prerelease)


def _command(executable: str, *args: str) -> subprocess.CompletedProcess[str]:
    return _run([executable, *args])


def _status(executable: str) -> dict[str, Any]:
    result = _command(executable, "status", "--json")
    if result.returncode not in (0, 1):
        raise UpdateError(result.stderr.strip() or "cannot inspect lodge status")
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise UpdateError("woltspace status did not return valid JSON") from exc


def _degraded(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    old_health = before.get("health") or {}
    new_health = after.get("health") or {}
    old_connectors = old_health.get("connectors") or before.get("connectors") or []
    new_connectors = new_health.get("connectors") or after.get("connectors") or []
    old = {item.get("name"): item for item in old_connectors}
    problems = []
    for item in new_connectors:
        prior = old.get(item.get("name"), {})
        if item.get("state") != "running" and item.get("state") != prior.get("state"):
            problems.append(f"connector {item.get('name')}: {item.get('state')}")
        if item.get("error") and item.get("error") != prior.get("error"):
            problems.append(f"connector {item.get('name')}: {item.get('error')}")
    old_orphans = len((old_health.get("adoption") or {}).get("orphaned", []))
    new_orphans = len((new_health.get("adoption") or {}).get("orphaned", []))
    if new_orphans > old_orphans:
        problems.append(f"session adoption: {new_orphans} orphaned (was {old_orphans})")
    return problems


def update(args: Any) -> int:
    as_json = bool(args.json)

    def emit(payload: dict[str, Any], message: str = "") -> None:
        print(json.dumps(payload, indent=2) if as_json else message)

    try:
        layout = RuntimeLayout.from_env()
        container_flag = os.environ.get("WOLTSPACE_CONTAINER", "").strip().lower()
        if layout.isolation != "host" or container_flag in {"1", "true", "yes"}:
            raise UpdateError("container/external lodges must be updated by host tooling")
        installed = Version(__version__)
        _uv_manages_exact_registry_install(installed)
        target = _target(_pypi(), args.version or "", bool(args.pre))
        if target.version < installed:
            raise UpdateError(f"refusing downgrade from {installed} to {target.version}")
        available = target.version > installed
        summary = {
            "ok": True,
            "installed": str(installed),
            "target": str(target.version),
            "update_available": available,
            "release_notes": RELEASE_URL.format(version=target.version),
        }
        if args.check or not available:
            emit(summary, f"installed {installed}; target {target.version}; " + ("update available" if available else "up to date"))
            return 0

        if not args.yes:
            print(f"Update Woltspace {installed} -> {target.version}", file=sys.stderr)
            print("The lodge goes offline for the install; tmux sessions survive.", file=sys.stderr)
            print(summary["release_notes"], file=sys.stderr)
            print("Continue? [y/N] ", end="", file=sys.stderr, flush=True)
            if input().strip().lower() not in {"y", "yes"}:
                emit({**summary, "ok": False, "cancelled": True}, "update cancelled")
                return 1

        executable = shutil.which("woltspace")
        if not executable:
            raise UpdateError("cannot find the installed woltspace executable")
        before = _status(executable)
        state = before.get("state")
        if state not in {"healthy", "stopped"}:
            raise UpdateError(f"lodge state is {state!r}; resolve it before updating")
        was_running = state == "healthy"
        if was_running:
            stopped = _command(executable, "stop")
            if stopped.returncode:
                raise UpdateError(stopped.stderr.strip() or "woltspace stop failed")
            if _status(executable).get("state") != "stopped":
                raise UpdateError("the lodge did not stop; installation was not attempted")

        install = ["uv", "tool", "install", "--force", f"woltspace=={target.version}"]
        if target.prerelease:
            install.extend(["--prerelease", "allow"])
        installed_result = _run(install)
        start_result: subprocess.CompletedProcess[str] | None = None
        if was_running:
            new_executable = shutil.which("woltspace") or executable
            start_result = _command(new_executable, "start")
        if installed_result.returncode:
            details = installed_result.stderr.strip() or "uv tool install failed"
            if start_result is not None and start_result.returncode:
                details += "; lodge restart also failed: " + (start_result.stderr.strip() or "unknown error")
            raise UpdateError(details)
        if start_result is not None and start_result.returncode:
            raise UpdateError(start_result.stderr.strip() or "updated, but lodge restart failed")

        new_executable = shutil.which("woltspace") or executable
        observed = _command(new_executable, "--version")
        if observed.returncode or str(target.version) not in observed.stdout.split():
            raise UpdateError(f"version verification failed: {observed.stdout.strip() or observed.stderr.strip()}")
        after = _status(new_executable)
        if was_running:
            deadline = time.monotonic() + 30
            while after.get("state") != "healthy" and time.monotonic() < deadline:
                time.sleep(5)
                after = _status(new_executable)
        problems = _degraded(before, after)
        if was_running and after.get("state") != "healthy":
            problems.append(f"lodge state: {after.get('state')}")
        result = {**summary, "health": after.get("state"), "degraded": problems}
        emit(result, f"updated {installed} -> {target.version}; lodge {after.get('state')}" + (f"; degraded: {', '.join(problems)}" if problems else ""))
        return 1 if problems else 0
    except UpdateError as exc:
        emit({"ok": False, "error": str(exc)}, f"update failed: {exc}")
        return 1
