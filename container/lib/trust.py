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

Both writers **fail open**. Trust is a convenience — it saves the human one
dialog — and it sits on the critical path of every spawn. A config we cannot
read, decode, parse or lock is a reason to skip the write and let the session
launch, never a reason to take the session down with us. The worst outcome of
failing open is the pre-fix world: somebody answers a dialog.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import sys
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
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        # mkstemp is 0600; the file we are replacing may not be. Keep whatever
        # permissions the user's own config already carried.
        try:
            os.chmod(tmp, path.stat().st_mode & 0o7777)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _warn(message: str) -> None:
    """One line to stderr, then carry on. Never the end of a spawn."""
    print(f"woltspace: trust: {message}", file=sys.stderr)


@contextlib.contextmanager
def _exclusive(path: Path):
    """Hold an exclusive lock over ``path`` for the read-modify-write.

    Two spawns preparing different workdirs at the same moment both read the
    old config, both write their own entry, and the loser's entry evaporates.
    The lock lives in a sidecar next to the target rather than on the target
    itself, because the write is an `os.replace` — the inode we would have
    locked is not the inode that survives.

    `fcntl.flock` is the portable-enough primitive for the two runtimes we
    ship on (macOS and Linux) and costs no dependency. Yields True when the
    lock is held; yields False when it could not be taken, and the caller
    then declines to write — fail open, as ever.
    """
    lock_path = path.parent / f"{path.name}.woltspace-trust.lock"
    fd = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError as exc:
        if fd is not None:
            os.close(fd)
        _warn(f"could not lock {lock_path}: {exc} — skipping trust write")
        yield False
        return
    try:
        yield True
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def ensure_claude_dir_trusted(work_dir: str | Path, wolts_dir: str | Path) -> bool:
    """Pre-accept Claude's trust dialog for ``work_dir``. Returns True if written.

    A no-op — never a write — when ``work_dir`` is not inside ``wolts_dir``.
    That scope guard is the whole security story: woltspace auto-trusts the
    colony it was pointed at and nothing else.

    Idempotent by design: an entry that already carries both flags is left
    untouched, because every gratuitous rewrite of ~/.claude.json widens the
    clobber race with a claude process that is running right now.

    The read and the write happen under one exclusive lock, so two spawns
    landing together merge instead of overwriting one another.
    """
    target = _resolve_inside(work_dir, wolts_dir)
    if target is None:
        return False

    config = claude_config_path()
    with _exclusive(config) as locked:
        if not locked:
            return False
        return _write_claude_trust(config, str(target))


def _write_claude_trust(config: Path, key: str) -> bool:
    """The locked half of `ensure_claude_dir_trusted`."""
    try:
        data = json.loads(config.read_text()) if config.exists() else {}
    except OSError as exc:
        _warn(f"cannot read {config}: {exc} — skipping trust write")
        return False
    except UnicodeDecodeError:
        # Not UTF-8 at all. Decoding it our way and writing it back would
        # mangle bytes we never understood.
        _warn(f"{config} is not valid UTF-8 — skipping trust write")
        return False
    except json.JSONDecodeError as exc:
        # Half-written or hand-mangled: leave it be. A session that prompts is
        # a better outcome than a clobbered state file.
        _warn(f"{config} is not valid JSON ({exc.msg}) — skipping trust write")
        return False
    if not isinstance(data, dict):
        _warn(f"{config} is not a JSON object — skipping trust write")
        return False

    projects = data.setdefault("projects", {})
    if not isinstance(projects, dict):
        _warn(f'{config} has a non-object "projects" — skipping trust write')
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
    except OSError as exc:
        _warn(f"cannot write {config}: {exc} — skipping trust write")
        return False
    return True


_TRUST_LEVEL_ASSIGN = re.compile(r"^\s*trust_level\s*=")


def _toml_key(key: str) -> str:
    """``key`` as a TOML basic string, escapes and all.

    Nothing stops a native workdir from holding a `"` or a `\\` — macOS allows
    both in a directory name — and interpolating one raw produces a config.toml
    codex itself then chokes on, or a table keyed on a path nobody has. JSON's
    string escaping is a superset-compatible subset of TOML's for the
    characters a path can contain, so `json.dumps` is the escaper.
    """
    return json.dumps(key)


def _projects_table_key(line: str) -> str | None:
    """The project a `[projects."…"]` header names, or None if it is not one.

    Parsing the header line rather than string-matching it means we recognise
    the table however the user (or a previous codex) chose to quote the path —
    literal strings, escaped basic strings, extra whitespace.
    """
    stripped = line.strip()
    if not stripped.startswith("[") or stripped.startswith("[[") or not stripped.endswith("]"):
        return None
    try:
        parsed = tomllib.loads(stripped + "\n")
    except tomllib.TOMLDecodeError:
        return None
    projects = parsed.get("projects")
    if not isinstance(projects, dict) or len(projects) != 1:
        return None
    (name, body), = projects.items()
    # A deeper header like [projects."x".sub] nests a non-empty body; that is
    # a different table and not the one we are looking for.
    return name if isinstance(body, dict) and not body else None


def _codex_trust_state(text: str, key: str) -> str:
    """How ``text`` currently stands on ``key``.

    - ``trusted``  — already says so; nothing to do.
    - ``present``  — the table exists with some other (or no) trust_level.
    - ``absent``   — no such table; safe to append one.
    - ``unknown``  — the file does not parse as TOML but does carry a header
      for this key. Appending would stack a second table on top of a file we
      cannot read; we decline instead.
    """
    try:
        parsed = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, AttributeError):
        for line in text.splitlines():
            if _projects_table_key(line) == key:
                return "unknown"
        return "absent"
    projects = parsed.get("projects")
    entry = projects.get(key) if isinstance(projects, dict) else None
    if not isinstance(entry, dict):
        return "absent"
    if entry.get("trust_level") == "trusted":
        return "trusted"
    return "present"


def _codex_retrust(text: str, key: str) -> str | None:
    """Flip an existing project table to trusted, in place. None if we cannot.

    Targeted surgery rather than a re-serialise: config.toml is the user's
    file, and round-tripping it through a writer we do not have would cost
    them every comment and every bit of ordering they chose. We touch the one
    line that has to change and leave the rest byte for byte.
    """
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if _projects_table_key(line) == key), None)
    if start is None:
        # The parser found the key but no header line owns it — an inline
        # table, or a dotted key. Not a shape we will edit blind.
        return None

    end = len(lines)
    for j in range(start + 1, len(lines)):
        stripped = lines[j].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            end = j
            break

    for j in range(start + 1, end):
        if _TRUST_LEVEL_ASSIGN.match(lines[j]):
            newline = "\n" if lines[j].endswith("\n") else ""
            lines[j] = f'trust_level = "trusted"{newline}'
            return "".join(lines)

    if not lines[start].endswith("\n"):
        lines[start] += "\n"
    lines.insert(start + 1, 'trust_level = "trusted"\n')
    return "".join(lines)


def ensure_codex_dir_trusted(work_dir: str | Path, wolts_dir: str | Path) -> bool:
    """Pre-accept codex's trust dialog for ``work_dir``. Returns True if written.

    Same boundary as claude's: a no-op — never a write — when ``work_dir`` is
    not inside ``wolts_dir``.

    A missing table is appended, because that is exactly what codex itself
    does when a human accepts the dialog, and it leaves the rest of a config
    we do not own — comments, ordering, keys we have never heard of — byte for
    byte where the user put it. A table that already exists is *edited*, never
    appended a second time: TOML has no room for two `[projects."x"]` headers,
    and a config with two is a config codex refuses to start on. That is the
    difference between "untrusted, still asks" and "broken, never runs again",
    and the old append could produce the second from an untrusted entry or
    from two spawns racing. The read, the decision and the write now happen
    under one exclusive lock.

    CODEX_HOME is created if it is missing; codex hard-errors on an absent one.
    """
    target = _resolve_inside(work_dir, wolts_dir)
    if target is None:
        return False

    config = codex_config_path()
    key = str(target)
    with _exclusive(config) as locked:
        if not locked:
            return False

        try:
            text = config.read_text() if config.exists() else ""
        except OSError as exc:
            _warn(f"cannot read {config}: {exc} — skipping trust write")
            return False
        except UnicodeDecodeError:
            # Not something we can read as text, so not something we may append
            # to blind. A session that prompts beats a config we corrupted.
            _warn(f"{config} is not valid UTF-8 — skipping trust write")
            return False

        state = _codex_trust_state(text, key)
        if state == "trusted":
            return False
        if state == "unknown":
            _warn(
                f"{config} does not parse as TOML but already names {key} — "
                "skipping trust write"
            )
            return False

        if state == "present":
            rewritten = _codex_retrust(text, key)
            if rewritten is None:
                _warn(
                    f"{config} carries {key} in a shape we cannot edit safely — "
                    "skipping trust write"
                )
                return False
            try:
                _write_atomically(config, rewritten)
            except OSError as exc:
                _warn(f"cannot write {config}: {exc} — skipping trust write")
                return False
            return True

        try:
            config.parent.mkdir(parents=True, exist_ok=True)
            with config.open("a") as f:
                f.write(f'\n[projects.{_toml_key(key)}]\ntrust_level = "trusted"\n')
        except OSError as exc:
            _warn(f"cannot write {config}: {exc} — skipping trust write")
            return False
        return True
