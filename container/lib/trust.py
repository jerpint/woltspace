"""Workspace trust — so a headless spawn never parks on the dialog.

Claude Code will not start in a directory it has never been trusted for: it
paints the workspace-trust dialog and waits. A session we spawn headlessly
(lodge API, telegram, wolf cron) has nobody to answer it — the pane never even
renders, no transcript is written, and `status` cheerfully reports "running"
forever. Every fresh native install hits this once per wolt.

Installing woltspace and pointing it at a data root IS the trust decision. So
the data root is the boundary: a workdir inside `wolts_dir` is trusted for the
session automatically, and anything outside it still gets the dialog.

`container/bin/trust-dir` does the same job for the container's `wclaude`
wrapper and is left alone — both writers are idempotent and set the same two
keys, so in-container they simply agree. Natively, `wclaude` deliberately
touches nothing in the user's HOME; this is the one place that does, and it
writes two booleans for one directory inside the colony.

Codex has the same dialog and the same blind spot — it asks even with
`--dangerously-bypass-approvals-and-sandbox` (verified live, codex-cli
0.144.4). `container/bin/wcodex` preseeds its answer, but from below the
early `exec codex` the host path takes, so natively nothing preseeded it at
all. `ensure_codex_dir_trusted` closes that gap under the same boundary.
"""

from __future__ import annotations

import json
import os
import tempfile
import tomllib
from pathlib import Path

TRUST_FLAGS = {
    "hasTrustDialogAccepted": True,
    "hasCompletedProjectOnboarding": True,
}


def claude_config_path() -> Path:
    """Where Claude Code keeps its live state (and its trusted-project list)."""
    return Path.home() / ".claude.json"


def codex_config_path() -> Path:
    """Where codex keeps its config (and its trusted-project list).

    `$CODEX_HOME/config.toml`, defaulting to `~/.codex` — the same resolution
    `woltspace doctor` uses to find codex's auth. In the container `wcodex`
    repoints CODEX_HOME per wolt before codex ever runs; natively it is
    whatever the host exports, which is usually nothing at all.
    """
    codex_home = os.environ.get("CODEX_HOME") or (Path.home() / ".codex")
    return Path(codex_home).expanduser() / "config.toml"


def _resolve_inside(work_dir: str | Path, wolts_dir: str | Path) -> Path | None:
    """Resolve ``work_dir``, or None if it does not live inside ``wolts_dir``.

    The scope guard both trust writers share: woltspace auto-trusts the colony
    it was pointed at and nothing else.
    """
    try:
        target = Path(work_dir).expanduser().resolve()
        root = Path(wolts_dir).expanduser().resolve()
    except OSError:
        return None
    return target if target.is_relative_to(root) else None


def _write_atomically(path: Path, text: str) -> None:
    """Replace the file in one step — a crash mid-write must not truncate it.

    ~/.claude.json is Claude's own running state, not a config we own.
    """
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".claude.json.")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def ensure_claude_dir_trusted(work_dir: str | Path, wolts_dir: str | Path) -> bool:
    """Pre-accept Claude's trust dialog for ``work_dir``. Returns True if written.

    A no-op — never a write — when ``work_dir`` is not inside ``wolts_dir``.
    That scope guard is the whole security story: woltspace auto-trusts the
    colony it was pointed at and nothing else.

    Idempotent by design: an entry that already carries both flags is left
    untouched, because every gratuitous rewrite of ~/.claude.json widens the
    clobber race with a claude process that is running right now.
    """
    target = _resolve_inside(work_dir, wolts_dir)
    if target is None:
        return False

    config = claude_config_path()
    try:
        data = json.loads(config.read_text()) if config.exists() else {}
    except (OSError, json.JSONDecodeError):
        # Unreadable or half-written: leave it be. A session that prompts is a
        # better outcome than a clobbered state file.
        return False
    if not isinstance(data, dict):
        return False

    key = str(target)
    projects = data.setdefault("projects", {})
    if not isinstance(projects, dict):
        return False
    entry = projects.get(key)
    if isinstance(entry, dict) and all(entry.get(k) is True for k in TRUST_FLAGS):
        return False
    if not isinstance(entry, dict):
        entry = {}
        projects[key] = entry
    entry.update(TRUST_FLAGS)

    try:
        config.parent.mkdir(parents=True, exist_ok=True)
        _write_atomically(config, json.dumps(data, indent=2))
    except OSError:
        return False
    return True


def _codex_already_trusted(text: str, key: str) -> bool:
    """Does this config.toml already trust ``key``?

    Parsed when it parses; when it does not — a half-written or hand-edited
    file — fall back to spotting the exact header, which is the shape both
    codex and `wcodex` write.
    """
    try:
        projects = tomllib.loads(text).get("projects", {})
    except (tomllib.TOMLDecodeError, AttributeError):
        return f'[projects."{key}"]' in text
    entry = projects.get(key) if isinstance(projects, dict) else None
    return isinstance(entry, dict) and entry.get("trust_level") == "trusted"


def ensure_codex_dir_trusted(work_dir: str | Path, wolts_dir: str | Path) -> bool:
    """Pre-accept codex's trust dialog for ``work_dir``. Returns True if written.

    Same boundary as claude's: a no-op — never a write — when ``work_dir`` is
    not inside ``wolts_dir``.

    The block is appended rather than merged, because that is exactly what
    codex itself does when a human accepts the dialog, and it leaves the rest
    of a config we do not own — comments, ordering, keys we have never heard
    of — byte for byte where the user put it. CODEX_HOME is created if it is
    missing; codex hard-errors on an absent one.
    """
    target = _resolve_inside(work_dir, wolts_dir)
    if target is None:
        return False

    config = codex_config_path()
    try:
        text = config.read_text() if config.exists() else ""
    except (OSError, UnicodeDecodeError):
        # Not something we can read as text, so not something we may append
        # to blind. A session that prompts beats a config we corrupted.
        return False

    key = str(target)
    if _codex_already_trusted(text, key):
        return False

    try:
        config.parent.mkdir(parents=True, exist_ok=True)
        with config.open("a") as f:
            f.write(f'\n[projects."{key}"]\ntrust_level = "trusted"\n')
    except OSError:
        return False
    return True
