"""Claude workspace trust — so a headless spawn never parks on the dialog.

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
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

TRUST_FLAGS = {
    "hasTrustDialogAccepted": True,
    "hasCompletedProjectOnboarding": True,
}


def claude_config_path() -> Path:
    """Where Claude Code keeps its live state (and its trusted-project list)."""
    return Path.home() / ".claude.json"


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
    try:
        target = Path(work_dir).expanduser().resolve()
        root = Path(wolts_dir).expanduser().resolve()
    except OSError:
        return False
    if not target.is_relative_to(root):
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
