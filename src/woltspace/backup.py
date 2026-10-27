"""Snapshots of the data plane — the wolts directory, and nothing else.

The container era backed up a lodge by committing the container and rsyncing
the whole wolts directory beside it. Native mode has no container to commit,
and a bare copy drags every `node_modules`, virtualenv, and build cache along:
gigabytes of things a rebuild would recreate, wrapped around the few megabytes
that are actually irreplaceable.

What is irreplaceable is data: memory, sites, sparks, drafts, apps, `.env`,
`woltspace.json`, the registries under `.state`, the `.space` metadata, and the
git history inside a wolt's own repos. That is what goes in the archive. Caches
are named, excluded by name at any depth, and reported so the win is visible.

By default a backup is *slim*: every piece of context (memory, identity,
transcripts, skills, lodge config, git history) at any size, plus every other
file that is text or code, up to a size cap. Media, binaries, databases nobody
opted in, and the caches no name rule catches stay out, and every file left out
is listed in the archive by path and size, so the whole story survives even
where the bytes do not.
`full=True` keeps the wider rule: everything but the named junk.

Nothing here touches docker or takes a lock, and a credential's value is only
ever read on its way into `age`. A backup of a live colony is safe; a quiescent
one only gives a tidier registry.
"""

from __future__ import annotations

import fnmatch
import io
import json
import os
import re
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tarfile
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from . import __version__

#: Everything inside the archive lives under this prefix, so a restore lands a
#: single directory the human can point WOLTSPACE_WOLTS_DIR at, with the manifest beside
#: it rather than buried in the tree it describes.
ARCHIVE_ROOT = "wolts"
MANIFEST_NAME = "backup-manifest.json"

#: Directory names dropped wherever they appear. Every one of these is either a
#: package manager's download cache, a build product, or a working copy that a
#: checkout can recreate.
EXCLUDED_DIR_NAMES = frozenset({
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".cache",
    ".pytest_cache",
    "dist",
    "build",
    ".next",
    "target",
    ".worktui",
    ".npm",
})

#: Paths excluded by their tail rather than by a bare name. A directory called
#: `cache` is only junk when it is Claude's plugin cache; `worktrees` only when
#: it is a wolt's pile of throwaway checkouts; and `claude` only when it is the
#: agent CLI's own version store.
#:
#: `.local/share/claude` is the single largest thing in a lived-in colony —
#: 12.8GB of auto-downloaded CLI binaries across per-wolt HOMEs on the colony
#: this was measured against, redownloadable to the byte. It is scoped by path
#: on purpose: a wolt's own directory called `claude` is data, and only `.local`'s
#: `share/claude` goes. The rest of `.local` stays, `.local/share/opencode`
#: (session state, and big) very much included, and conversation transcripts live
#: in `.claude/projects`, which is kept.
EXCLUDED_PATH_SUFFIXES = (
    ".claude/plugins/cache",
    "wolt/worktrees",
    ".local/share/claude",
    ".codex/shell_snapshots",
    ".space/lodge.db-wal",
    ".space/lodge.db-shm",
)

#: The lodge's own database goes in as an in-memory snapshot: one member,
#: consistent even while the lodge writes. Its live `-wal` and `-shm` stay out
#: (above). Only the lodge's: another SQLite file's `-wal` holds committed rows,
#: so an app's database is archived as it lies, sidecars and all.
LODGE_DB = ".space/lodge.db"

#: `.codex/shell_snapshots` earns its place twice over. Codex snapshots the
#: shell environment into runnable `.sh` files, so every exported secret — the
#: telegram bot token, on three wolts of the colony this was swept — is baked
#: into them verbatim. They are also pure runtime scratch, rebuilt on the next
#: session. The rest of `.codex` (sessions, rollouts) is history, and stays.

#: At the top of the data root, a name means something else. `dist`, `build`,
#: `target` and friends are junk *inside* a project and perfectly good names
#: *for* a wolt or an app, and a backup that silently skipped a wolt called
#: `dist` would be worse than no backup at all. So depth 1 excludes nothing by
#: name except what the platform itself derives there — worktui's worktree
#: store. Everything else the platform puts at the root (`.space`, `.state`,
#: `.claude`, `.codex`, `apps`, `projects`, `woltspace.json`, `.env`) is data
#: and stays.
ROOT_EXCLUDED_DIR_NAMES = frozenset({".worktui"})

EXCLUDED_FILE_NAMES = frozenset({".DS_Store"})
EXCLUDED_FILE_SUFFIXES = (".pyc", ".sock")

#: The list of files a slim backup left out, one `path<TAB>kind<TAB>bytes<TAB>files`
#: row each, beside the manifest at the archive root.
LEFT_OUT_NAME = "backup-left-out.tsv"
_META_NAMES = frozenset({MANIFEST_NAME, LEFT_OUT_NAME})

#: Slim: context is kept at any size. A path is context when, from some point
#: down (so a nested lodge's wolts count too), it starts with one of these.
SLIM_CONTEXT_PREFIXES = (
    "wolt/memory/", "wolt/wolt.json", "wolt.json", "CLAUDE.md", "AGENTS.md",
    ".claude/projects/", ".claude-state/projects/", ".claude/skills/",
    ".agents/skills/", "skills/", ".claude/file-history/", ".claude/todos/",
    ".claude-state/file-history/", ".claude-state/todos/", ".codex/sessions/",
    ".codex/archived_sessions/", ".codex/state_", ".codex/memories_", ".codex/goals_",
    ".codex/queue_", ".local/share/opencode/",
)
#: Session transcripts: reproducibility is the point, so a slim backup keeps
#: every one, even inside a directory it otherwise drops (a checkout, a cache,
#: a scratch lodge under `.tmp`). Such a directory is entered for its
#: transcripts only; the rest of it is listed as left out.
SLIM_TRANSCRIPT_PREFIXES = (
    ".claude/projects/", ".claude-state/projects/", ".codex/sessions/",
    ".codex/archived_sessions/", ".local/share/opencode/",
)
#: ... and at the data root, the lodge's own config and state, `lodge.db`
#: included (it is the lodge, not an app's database). Logs are not context.
SLIM_ROOT_CONTEXT = (".space/", ".state/", "woltspace.json")
SLIM_ROOT_SKIPPED = (".space/logs/",)

#: Slim: anything else is kept only when it is text or code of at most
#: SLIM_TEXT_MAX bytes. A file with no suffix (Dockerfile, LICENSE, a git ref)
#: is kept up to SLIM_NO_SUFFIX_MAX.
SLIM_TEXT_MAX = 5_000_000
SLIM_NO_SUFFIX_MAX = 1_000_000
SLIM_TEXT_SUFFIXES = frozenset({
    ".md", ".mdx", ".txt", ".rst", ".html", ".htm", ".css", ".scss", ".js", ".mjs",
    ".cjs", ".ts", ".tsx", ".jsx", ".astro", ".vue", ".svelte", ".json", ".jsonl",
    ".ipynb", ".py", ".sh", ".bash", ".zsh", ".fish", ".toml", ".yaml", ".yml",
    ".ini", ".cfg", ".conf", ".csv", ".tsv", ".sql", ".go", ".rs", ".swift", ".kt",
    ".java", ".c", ".h", ".cpp", ".hpp", ".rb", ".lua", ".svg", ".xml", ".lock",
    ".gitignore", ".gitattributes", ".gitmodules", ".dockerignore", ".plist",
    ".service", ".tmpl", ".j2", ".example", ".sample", ".tex", ".bib",
})
#: Build recipes are code whatever their size, and have no suffix to say so.
SLIM_BUILD_NAMES = frozenset({
    "Makefile", "GNUmakefile", "Dockerfile", "Containerfile", "Procfile", "Justfile",
    "justfile", "Rakefile", "Gemfile", "Brewfile", "Vagrantfile", "Caddyfile",
})
#: Slim leaves databases out: an app's database is state, but it can grow
#: without bound. A wolt or an app opts a path back in from its own manifest
#: (`woltspace.json`, or `wolt/wolt.json` for a wolt, in that directory):
#: `"backup": {"keep": ["data/app.db"]}`, paths relative to the directory,
#: shell globs allowed. A kept path is kept at any size, with the SQLite
#: companions beside it.
BACKUP_MANIFEST_NAMES = ("woltspace.json", "wolt/wolt.json")
SQLITE_COMPANIONS = ("-wal", "-shm", "-journal")
_MANIFEST_READ_MAX = 1_000_000

#: Slim: directories dropped below the top level, on top of the excludes above.
#: Package and download caches, browser builds, and checkouts (`worktrees` at
#: any depth, not only `wolt/worktrees`). A `worktrees` inside `.git` is git's
#: own bookkeeping and stays.
SLIM_EXCLUDED_DIR_NAMES = frozenset({
    "worktrees", "cache", "_cacache", "npm-cache", ".npm-cache", ".uv-cache",
    "uv-cache", "archive-v0", ".uv-python", "site-packages", "ms-playwright",
    "browsers", ".bun", ".tmp",
})

#: Credentials are withheld, not backed up. Two reasons, and the first is the
#: one that bites: a rotating OAuth chain has exactly one live owner, so
#: restoring a *stale* credentials file replays a spent refresh token, and
#: reuse-detection answers by revoking the chain — the backup takes down the
#: colony it was supposed to protect. The second is plain: an archive sitting
#: on a disk or in a bucket should not be a bearer token at rest.
#:
#: These rules are explicit names and paths. Nothing here sniffs content: a
#: heuristic that reads files looking for secrets is a heuristic that reads
#: secrets, and one that guesses wrong drops data.
SECRET_RULES = (
    ("dotenv", ".env or .env.* at any depth, except .example / .sample templates"),
    ("claude-credentials", ".credentials.json* directly inside a .claude directory"),
    ("codex-auth", "auth.json directly inside a .codex directory"),
    ("platform-config", ".space/platform/config.json at the data root (bot tokens)"),
)

#: The withheld files that travel inside the archive when it is encrypted.
#: Never the OAuth chains: encrypted or not, a restored stale one revokes the
#: live one.
ENCRYPTED_RULES = ("dotenv", "platform-config")
AGE_RECIPIENT_ENV = "WOLTSPACE_BACKUP_AGE_RECIPIENT"
AGE_HEADER = b"age-encryption.org/v1\n"


#: `.env.local`, `.env.production`, `.env.staging` — in every app ecosystem a
#: wolt builds in, a suffixed dotenv holds real values. `.env.example` and
#: `.env.sample` are the two conventions for the opposite: a checked-in
#: template with the values blanked out.
DOTENV_TEMPLATE_SUFFIXES = (".example", ".sample")


def secret_rule(rel: str) -> str | None:
    """Which secret rule a path trips, if any. Names and paths only."""
    path = PurePosixPath(rel)
    name = path.name
    parent = path.parent.name
    if name == ".env" or (
        name.startswith(".env.") and not name.endswith(DOTENV_TEMPLATE_SUFFIXES)
    ):
        return "dotenv"
    if parent == ".claude" and name.startswith(".credentials.json"):
        return "claude-credentials"
    if parent == ".codex" and name == "auth.json":
        return "codex-auth"
    if rel == ".space/platform/config.json":
        return "platform-config"
    return None

#: `.git` is deliberately absent from all of the above: a wolt's history is
#: data, and a snapshot that loses it is not a snapshot.


def default_tag() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def archive_name(tag: str, encrypted: bool = False) -> str:
    return f"woltspace-backup-{tag}.tar.gz" + (".age" if encrypted else "")


@dataclass
class Entry:
    """One member of the archive, resolved before a byte is written."""

    path: Path
    arcname: str
    kind: str  # "dir" | "file" | "symlink"
    size: int = 0


@dataclass
class Scan:
    entries: list[Entry] = field(default_factory=list)
    included_bytes: int = 0
    included_files: int = 0
    excluded_bytes: int = 0
    excluded_files: int = 0
    withheld: list[dict] = field(default_factory=list)
    unreadable: list[dict] = field(default_factory=list)
    wolts: list[dict] = field(default_factory=list)
    full: bool = True
    #: What a slim backup left out: {"path", "kind": "file"|"dir", "bytes", "files"}.
    left_out: list[dict] = field(default_factory=list)
    #: Slim opt-ins found on the way: {directory: [patterns]}.
    opted_in: dict[str, list[str]] = field(default_factory=dict)

    @property
    def scanned_bytes(self) -> int:
        return self.included_bytes + self.excluded_bytes


@dataclass
class BackupResult:
    archive: Path
    manifest: dict
    verified: bool
    scan: Scan


@dataclass
class RestoreResult:
    target: Path
    wolts_dir: Path
    manifest: dict
    entries: int
    #: What the restore changed so the copy cannot act on the source: see `disarm`.
    disarmed: dict = field(default_factory=dict)


def _rel(root: Path, path: Path) -> str:
    return PurePosixPath(os.path.relpath(path, root)).as_posix()


def _path_excluded(rel: str) -> bool:
    return any(
        rel == suffix or rel.endswith("/" + suffix) for suffix in EXCLUDED_PATH_SUFFIXES
    )


def is_wolt_dir(path: Path) -> bool:
    """The discovery rule from `container/lib/wolts.py`, held one notch looser.

    Discovery globs `*/wolt/wolt.json`. Here anything that even looks like a
    wolt counts, because the cost of a false positive is a cache getting backed
    up and the cost of a false negative is a wolt going missing.
    """
    path = Path(path)
    return (
        (path / "wolt" / "wolt.json").is_file()
        or (path / "wolt.json").is_file()
        or (path / "wolt").is_dir()
    )


def _dir_excluded(name: str, rel: str, path: Path) -> bool:
    """Whether a directory is junk — depth and wolt-ness both get a veto."""
    if _under_root_exclude(rel):
        return True  # the platform's own derived dirs outrank every veto
    if is_wolt_dir(path):
        return False  # a wolt is never junk, wherever it happens to sit
    if "/" not in rel:  # a direct child of the data root
        return name in ROOT_EXCLUDED_DIR_NAMES or _path_excluded(rel)
    return name in EXCLUDED_DIR_NAMES or _path_excluded(rel)


def _file_excluded(name: str, rel: str) -> bool:
    if name in EXCLUDED_FILE_NAMES or name.endswith(EXCLUDED_FILE_SUFFIXES):
        return True
    return _path_excluded(rel)


def _under_root_exclude(rel: str) -> bool:
    """Whether a path lives inside one of the root's derived directories."""
    return rel.split("/", 1)[0] in ROOT_EXCLUDED_DIR_NAMES


def _slim_dir_dropped(name: str, rel: str, path: Path) -> bool:
    """Whether a slim backup leaves a whole directory out."""
    parts = rel.split("/")
    if len(parts) < 2 or ".git" in parts[:-1] or is_wolt_dir(path):
        return False
    if name in SLIM_EXCLUDED_DIR_NAMES or name.startswith("node_modules"):
        return True
    return name.startswith("python3") and parts[-2] == "lib"  # an installed venv or tool


def _has_prefix_tail(rel: str, prefixes: tuple[str, ...]) -> bool:
    parts = rel.split("/")
    return any("/".join(parts[start:]).startswith(prefixes) for start in range(len(parts)))


def is_transcript(rel: str) -> bool:
    return _has_prefix_tail(rel, SLIM_TRANSCRIPT_PREFIXES)


def _holds_transcripts(path: Path, rel: str) -> bool:
    """Whether any file under a directory about to be dropped is a transcript."""
    if is_transcript(rel + "/"):
        return True
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        here = rel + dirpath[len(str(path)):].replace(os.sep, "/")
        for name in filenames:
            if is_transcript(f"{here}/{name}"):
                return True
    return False


def backup_keep_patterns(directory: Path) -> list[str]:
    """The paths a directory's own manifest opts into a slim backup."""
    patterns: list[str] = []
    for name in BACKUP_MANIFEST_NAMES:
        path = directory / name
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > _MANIFEST_READ_MAX:
                continue
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        keep = (data.get("backup") or {}).get("keep") if isinstance(data, dict) else None
        if not isinstance(keep, list):
            continue
        for pattern in keep:
            if not isinstance(pattern, str) or not pattern.strip():
                continue
            pure = PurePosixPath(pattern.strip().rstrip("/"))
            if pure.is_absolute() or ".." in pure.parts:
                continue  # a manifest only speaks for its own directory
            patterns.append(pure.as_posix())
    return patterns


def _opted_in(rel: str, keeps: dict[str, list[str]]) -> bool:
    for base, patterns in keeps.items():
        if base and not rel.startswith(base + "/"):
            continue
        tail = rel[len(base) + 1:] if base else rel
        for candidate in (tail, *(tail[: -len(c)] for c in SQLITE_COMPANIONS if tail.endswith(c))):
            if any(fnmatch.fnmatchcase(candidate, pattern) for pattern in patterns):
                return True
    return False


def slim_keeps(rel: str, size: int) -> bool:
    """Whether a slim backup keeps a file: all context, plus text up to a cap."""
    parts = rel.split("/")
    if ".git" in parts[:-1]:
        return True  # history is data
    if rel.startswith(SLIM_ROOT_CONTEXT):
        return not rel.startswith(SLIM_ROOT_SKIPPED)
    for start in range(len(parts)):
        if "/".join(parts[start:]).startswith(SLIM_CONTEXT_PREFIXES):
            return True
    if parts[-1] in SLIM_BUILD_NAMES:
        return True
    suffix = PurePosixPath(parts[-1]).suffix.lower()
    if suffix in SLIM_TEXT_SUFFIXES:
        return size <= SLIM_TEXT_MAX
    return suffix == "" and size <= SLIM_NO_SUFFIX_MAX


def _probe_excluded(path: Path) -> tuple[int, int, bool]:
    """Size a subtree about to be dropped — and check it hides no wolt.

    The walk is happening anyway (the summary names what it left behind), so it
    also watches for a `wolt.json`. A wolt filed under a junk-sounding
    directory is still a wolt, and the caller keeps the whole subtree rather
    than lose it. Backing up too much is a cost; backing up too little is a
    silent loss.
    """
    total = 0
    count = 0
    has_wolt = False
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        for name in list(dirnames):
            if Path(dirpath, name).is_symlink():
                dirnames.remove(name)
        if "wolt.json" in filenames:
            has_wolt = True
        for name in filenames:
            try:
                info = os.lstat(Path(dirpath, name))
            except OSError:
                continue
            count += 1
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
    return total, count, has_wolt


def scan_tree(wolts_dir: Path, *, full: bool = True) -> Scan:
    """Walk the data root once, deciding membership before anything is written.

    Symlinks are never followed. The plugin-delivery links point into the
    installed wheel, and a backup that chased them would swallow the platform
    it is not supposed to be backing up — they are archived as links, and a
    restore recreates links.
    """
    wolts_dir = Path(wolts_dir)
    scan = Scan(full=full)
    per_wolt: dict[str, dict] = {}

    def note(rel: str, size: int, files: int, included: bool) -> None:
        top = rel.split("/", 1)[0]
        if top not in per_wolt:
            return
        if included:
            per_wolt[top]["bytes"] += size
            per_wolt[top]["files"] += files
        else:
            per_wolt[top]["excluded_bytes"] += size

    for child in sorted(wolts_dir.iterdir()):
        if child.is_dir() and not child.is_symlink() and is_wolt_dir(child):
            per_wolt[child.name] = {
                "name": child.name, "bytes": 0, "files": 0, "excluded_bytes": 0,
            }

    def on_error(exc: OSError) -> None:
        scan.unreadable.append({
            "path": _rel(wolts_dir, Path(getattr(exc, "filename", "") or wolts_dir)),
            "error": exc.strerror or str(exc),
        })

    # Slim: directories it would drop but entered for their transcripts alone.
    transcripts_only: list[str] = []

    def in_transcripts_only(rel: str) -> bool:
        return any(rel.startswith(prefix + "/") for prefix in transcripts_only)

    # Slim: paths a manifest opted in, by the directory that holds the manifest.
    keeps: dict[str, list[str]] = {}

    def drop_dir(rel: str, child: Path) -> None:
        size, files, _ = _probe_excluded(child)
        scan.excluded_bytes += size
        scan.excluded_files += files
        scan.left_out.append({"path": rel, "kind": "dir", "bytes": size, "files": files})
        note(rel, size, files, included=False)

    for dirpath, dirnames, filenames in os.walk(wolts_dir, followlinks=False, onerror=on_error):
        here = Path(dirpath)
        if here != wolts_dir:
            scan.entries.append(Entry(here, f"{ARCHIVE_ROOT}/{_rel(wolts_dir, here)}", "dir"))
        if not full:
            patterns = backup_keep_patterns(here)
            if patterns:
                keeps["" if here == wolts_dir else _rel(wolts_dir, here)] = patterns

        for name in sorted(dirnames):
            child = here / name
            rel = _rel(wolts_dir, child)
            if child.is_symlink():
                # A symlinked directory is a link, not a subtree: archive the
                # link itself and never descend through it.
                dirnames.remove(name)
                scan.entries.append(Entry(child, f"{ARCHIVE_ROOT}/{rel}", "symlink"))
                continue
            junk = _dir_excluded(name, rel, child)
            probe = _probe_excluded(child) if junk else None
            # A wolt lives down there: the rescue below keeps the whole subtree.
            rescued = bool(probe and probe[2] and not _under_root_exclude(rel))
            if not full and not rescued and (
                in_transcripts_only(rel) or junk or _slim_dir_dropped(name, rel, child)
            ):
                if in_transcripts_only(rel) and is_transcript(rel + "/"):
                    continue  # inside a transcript store: every file is kept
                if _holds_transcripts(child, rel):
                    if not in_transcripts_only(rel):
                        transcripts_only.append(rel)
                    continue
                if not junk or in_transcripts_only(rel):
                    dirnames.remove(name)
                    drop_dir(rel, child)
                    continue
            if junk:
                size, files, has_wolt = probe
                # The rescue is for a wolt filed under a junk-sounding name. It
                # is NOT for `.worktui`, whose worktrees hold *copies* of wolts,
                # wolt.json and all — a copy of a wolt is not the wolt, and
                # resurrecting one would drag back the tree the root rule drops
                # on purpose.
                if has_wolt and not _under_root_exclude(rel):
                    continue  # a wolt lives down there; the subtree stays
                dirnames.remove(name)
                scan.excluded_bytes += size
                scan.excluded_files += files
                note(rel, size, files, included=False)
        dirnames[:] = [name for name in sorted(dirnames)]

        for name in sorted(filenames):
            child = here / name
            rel = _rel(wolts_dir, child)
            rule = secret_rule(rel)
            if rule:
                # Withheld, not excluded: the human is told to put it back.
                scan.withheld.append({"path": rel, "rule": rule})
                continue
            if _file_excluded(name, rel):
                try:
                    size = os.lstat(child).st_size
                except OSError:
                    size = 0
                scan.excluded_bytes += size
                scan.excluded_files += 1
                note(rel, size, 1, included=False)
                continue
            try:
                info = os.lstat(child)
            except OSError as exc:
                scan.unreadable.append({"path": rel, "error": exc.strerror or str(exc)})
                continue
            if stat.S_ISLNK(info.st_mode):
                scan.entries.append(Entry(child, f"{ARCHIVE_ROOT}/{rel}", "symlink"))
                continue
            if not stat.S_ISREG(info.st_mode):
                # Sockets, fifos, devices: tmux leaves them lying around and
                # none of them mean anything once restored.
                scan.unreadable.append({"path": rel, "error": "not a regular file"})
                continue
            if not full and (
                not is_transcript(rel) if in_transcripts_only(rel)
                else not (slim_keeps(rel, info.st_size) or _opted_in(rel, keeps))
            ):
                scan.excluded_bytes += info.st_size
                scan.excluded_files += 1
                scan.left_out.append({"path": rel, "kind": "file", "bytes": info.st_size, "files": 1})
                note(rel, info.st_size, 1, included=False)
                continue
            if not os.access(child, os.R_OK):
                scan.unreadable.append({"path": rel, "error": "unreadable"})
                continue
            scan.entries.append(Entry(child, f"{ARCHIVE_ROOT}/{rel}", "file", info.st_size))
            scan.included_bytes += info.st_size
            scan.included_files += 1
            note(rel, info.st_size, 1, included=True)

    scan.wolts = [per_wolt[name] for name in sorted(per_wolt)]
    scan.opted_in = keeps
    return scan


def build_manifest(wolts_dir: Path, tag: str, scan: Scan) -> dict:
    """Describe the snapshot without ever reading a secret's value.

    Credentials are not in the archive at all, and the `withheld` section names
    the paths that were left behind — paths only. Nothing in this function
    opens a file.
    """
    return {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tag": tag,
        "host": socket.gethostname(),
        "woltspace_version": __version__,
        "source": str(wolts_dir),
        "archive_root": ARCHIVE_ROOT,
        "wolts": scan.wolts,
        "entries": len(scan.entries),
        "withheld": {
            "why": (
                "credentials are never archived: a restored stale OAuth chain "
                "trips reuse-detection and revokes the live one, and an archive "
                "at rest should not be a bearer token"
            ),
            "rules": [{"rule": rule, "matches": how} for rule, how in SECRET_RULES],
            "paths": scan.withheld,
        },
        "totals": {
            "scanned_bytes": scan.scanned_bytes,
            "included_bytes": scan.included_bytes,
            "included_files": scan.included_files,
            "excluded_bytes": scan.excluded_bytes,
            "excluded_files": scan.excluded_files,
        },
        "excludes": {
            "by_dir_name": {
                "below_root": sorted(EXCLUDED_DIR_NAMES),
                "at_root": sorted(ROOT_EXCLUDED_DIR_NAMES),
                "rule": (
                    "a bare directory name is junk only below the top level; "
                    "the root drops only what the platform derives there"
                ),
            },
            "by_path": {
                "suffixes": list(EXCLUDED_PATH_SUFFIXES),
                "rule": "matched on the whole path tail, never on the bare name",
            },
            "by_file_name": sorted(EXCLUDED_FILE_NAMES),
            "by_file_suffix": list(EXCLUDED_FILE_SUFFIXES),
            "never_excluded": {
                "wolt_dirs": "a wolt, or any subtree containing one, at any depth",
                "git": "history is data",
                "symlinks": "archived as links; never followed",
            },
        },
        "unreadable": scan.unreadable,
        "mode": "full" if scan.full else "slim",
        **({} if scan.full else {"left_out": _left_out_summary(scan)}),
    }


def _left_out_summary(scan: Scan) -> dict:
    by_top: dict[str, int] = {}
    for item in scan.left_out:
        top = "/".join(item["path"].split("/")[:2])
        by_top[top] = by_top.get(top, 0) + item["bytes"]
    return {
        "why": (
            "a slim backup keeps all context and code; media, binaries, databases, "
            "caches and checkouts are named here, not archived"
        ),
        "listed_in": LEFT_OUT_NAME,
        "bytes": sum(item["bytes"] for item in scan.left_out),
        "files": sum(item["files"] for item in scan.left_out),
        "by_top": dict(sorted(by_top.items(), key=lambda kv: -kv[1])),
        "rules": {
            "context_kept_at_any_size": list(SLIM_CONTEXT_PREFIXES),
            "root_context": list(SLIM_ROOT_CONTEXT),
            "root_skipped": list(SLIM_ROOT_SKIPPED),
            "git": "every file inside a .git directory is kept",
            "text_suffixes": sorted(SLIM_TEXT_SUFFIXES),
            "text_max_bytes": SLIM_TEXT_MAX,
            "no_suffix_max_bytes": SLIM_NO_SUFFIX_MAX,
            "build_files_kept_at_any_size": sorted(SLIM_BUILD_NAMES),
            "opt_in": (
                '"backup": {"keep": [paths]} in a directory\'s woltspace.json '
                "(or a wolt's wolt/wolt.json) keeps those paths at any size, "
                "with their SQLite -wal/-shm/-journal companions"
            ),
            "opted_in": {base or ".": patterns for base, patterns in scan.opted_in.items()},
            "dropped_dir_names": sorted(SLIM_EXCLUDED_DIR_NAMES)
            + ["node_modules*", "lib/python3*"],
        },
    }


def _left_out_tsv(scan: Scan) -> bytes:
    rows = ["path\tkind\tbytes\tfiles"]
    rows += [
        f"{item['path']}\t{item['kind']}\t{item['bytes']}\t{item['files']}"
        for item in scan.left_out
    ]
    return ("\n".join(rows) + "\n").encode()


def verify_archive(archive: Path) -> dict:
    """Re-open what was just written and prove it is readable.

    A backup nobody has opened is a rumour. This parses the manifest back out
    of the archive, checks the member count against what the manifest claims,
    and spot-reads one wolt.json to prove the payload survived the round trip.
    """
    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
        payload = tar.extractfile(MANIFEST_NAME)
        if payload is None:
            raise ValueError(f"{archive}: manifest missing")
        manifest = json.loads(payload.read().decode())
        counted = len([name for name in names if name not in _META_NAMES])
        if counted != manifest["entries"]:
            raise ValueError(
                f"{archive}: manifest claims {manifest['entries']} entries, archive holds {counted}"
            )
        for name in names:
            if name.endswith("/wolt/wolt.json"):
                spot = tar.extractfile(name)
                if spot is None:
                    raise ValueError(f"{archive}: {name} is not readable")
                json.loads(spot.read().decode())
                break
    return manifest


def _add_entry(tar: tarfile.TarFile, entry: Entry) -> None:
    """Add one member — and never as a hard link.

    A colony's kept data *does* contain hard links: Claude's `file-history`
    dedupes identical file versions across sessions that way. `TarFile.add`
    remembers inodes, so the second name for one inode becomes a LNK member —
    and this module's own restore refuses LNK members, which meant a backup
    could write an archive it would not accept back.

    Rather than soften the restore, every regular file is stored whole. The
    duplication is bounded: the heavy hard-link populations on a real colony
    (uv caches, virtualenvs, worktrees) all sit in excluded directories, and the
    kept ones are tens of megabytes. What comes back out is two independent
    files with identical content, which is what a restored backup should be.
    """
    info = tar.gettarinfo(str(entry.path), entry.arcname)
    if info is None:
        raise OSError(f"unsupported file type: {entry.path}")
    if info.islnk():
        info.type = tarfile.REGTYPE
        info.linkname = ""
        info.size = os.lstat(entry.path).st_size
    if info.isreg():
        with open(entry.path, "rb") as payload:
            tar.addfile(info, payload)
    else:
        tar.addfile(info)


def snapshot_lodge_db(db: Path) -> tuple[bytes, dict]:
    """A consistent copy of the lodge database, in memory, and its row counts.

    The backup changes no rows: it first folds the WAL into the file (a
    TRUNCATE checkpoint, skipped with one line on stderr when busy or failed),
    then SQLite's backup API takes a consistent cut even mid-transaction, WAL
    rows included, every row kept. The copy never touches the disk.
    """
    try:
        with closing(sqlite3.connect(f"{Path(db).as_uri()}?mode=rw", uri=True, timeout=1)) as conn:
            busy = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
        problem = "busy" if busy else None
    except sqlite3.Error as exc:
        problem = str(exc)
    if problem:
        print(f"woltspace backup: lodge.db WAL checkpoint skipped ({problem})", file=sys.stderr)
    source = sqlite3.connect(f"{Path(db).as_uri()}?mode=ro", uri=True)
    copy = sqlite3.connect(":memory:", isolation_level=None)
    try:
        source.backup(copy)
        names = [row[0] for row in copy.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )]
        tables = {
            name: copy.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
            for name in names
        }
        return copy.serialize(), {"tables": tables}
    finally:
        copy.close()
        source.close()


def _owner_only(path: Path) -> int:
    """Open `path` for writing as 0600, whatever the umask or an older file's mode."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    return fd


def _encryption() -> tuple[str, str] | None:
    """`(age, recipient)` when the owner asked for encryption, else None.

    A recipient without `age` is an error, never a quiet fall back to a
    plaintext archive of the whole lodge.
    """
    recipient = os.environ.get(AGE_RECIPIENT_ENV, "").strip()
    if not recipient:
        return None
    age = shutil.which("age")
    if not age:
        raise OSError(
            f"{AGE_RECIPIENT_ENV} is set but age is not on PATH — "
            "refusing to write the lodge in the clear"
        )
    return age, recipient


def keep_secrets(wolts_dir: Path, scan: Scan) -> None:
    """Move the `.env` files and bot tokens from withheld into the archive.

    Only for an encrypted archive. A `.env` that is a symlink could point at
    anything: it is never read through, and stays on the restore checklist.
    """
    for item in scan.withheld:
        if item["rule"] not in ENCRYPTED_RULES:
            continue
        if (wolts_dir / item["path"]).is_symlink():
            scan.unreadable.append({"path": item["path"], "error": "symlink, not followed"})
            continue
        scan.entries.append(Entry(wolts_dir / item["path"], f"{ARCHIVE_ROOT}/{item['path']}", "file"))


def create_backup(
    wolts_dir: Path | str, *, out_dir: Path | str | None = None, tag: str | None = None,
    full: bool = False,
) -> BackupResult:
    """Write `woltspace-backup-<tag>.tar.gz[.age]` and verify it before returning.

    Slim unless `full`: see the module docstring. With a recipient the tar is
    streamed through `age`, secrets included, into a file opened 0600 before
    age writes a byte: no plaintext reaches the disk.
    """
    wolts_dir = Path(wolts_dir).expanduser().resolve(strict=False)
    if not wolts_dir.is_dir():
        raise FileNotFoundError(f"no wolts directory at {wolts_dir}")
    tag = tag or default_tag()
    out = Path(out_dir).expanduser().resolve(strict=False) if out_dir else wolts_dir.parent
    out.mkdir(parents=True, exist_ok=True)
    encryption = _encryption()
    archive = out / archive_name(tag, encrypted=bool(encryption))

    scan = scan_tree(wolts_dir, full=full)
    for item in scan.unreadable:
        if item["path"] in (".space", LODGE_DB):
            raise ValueError(f"lodge.db could not be read: {item['path']}: {item['error']}")
    if encryption:
        keep_secrets(wolts_dir, scan)
        secrets = {"present": True, "recipient": encryption[1]}
    else:
        secrets = {"present": False, "reason": f"{AGE_RECIPIENT_ENV} is not set"}
    manifest = build_manifest(wolts_dir, tag, scan)
    manifest["secrets"] = secrets

    fd = _owner_only(archive)
    age = None
    try:
        if encryption:
            try:
                age = subprocess.Popen(
                    [encryption[0], "-r", encryption[1]],
                    stdin=subprocess.PIPE, stdout=fd, stderr=subprocess.PIPE,
                )
            finally:
                os.close(fd)
            sink = age.stdin
        else:
            sink = os.fdopen(fd, "wb")
        with sink, tarfile.open(fileobj=sink, mode="w|gz") as tar:
            _write_members(tar, scan, manifest)
        if age:
            error = age.stderr.read().decode(errors="replace").strip()
            if age.wait() != 0:
                raise OSError(f"age could not encrypt the archive: {error}")
    except BaseException as exc:
        archive.unlink(missing_ok=True)
        if age is None:
            raise
        if age.poll() is None:
            age.kill()
        error = age.stderr.read().decode(errors="replace").strip()
        age.wait()
        if isinstance(exc, OSError) and error:
            # A broken pipe means age gave up first; its stderr says why.
            raise OSError(f"age could not encrypt the archive: {error}") from exc
        raise

    if encryption:
        with archive.open("rb") as sealed:
            if sealed.read(len(AGE_HEADER)) != AGE_HEADER:
                raise ValueError(f"{archive}: not an age file")
    else:
        verify_archive(archive)
    return BackupResult(archive=archive, manifest=manifest, verified=True, scan=scan)


def _write_members(tar: tarfile.TarFile, scan: Scan, manifest: dict) -> None:
    for entry in scan.entries:
        if entry.arcname == f"{ARCHIVE_ROOT}/{LODGE_DB}":
            # The lodge's state is the one file a backup cannot do without.
            try:
                data, manifest["lodge_db"] = snapshot_lodge_db(entry.path)
                info = tar.gettarinfo(str(entry.path), entry.arcname)
            except (OSError, sqlite3.Error) as exc:
                raise ValueError(f"lodge.db could not be snapshotted: {exc}") from exc
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
            continue
        try:
            _add_entry(tar, entry)
        except OSError as exc:
            # A file that vanished or turned unreadable between the scan
            # and the write is a warning, never a failed backup.
            manifest["unreadable"].append({
                "path": entry.arcname[len(ARCHIVE_ROOT) + 1:],
                "error": getattr(exc, "strerror", None) or str(exc),
            })
            manifest["entries"] -= 1
    meta = [(MANIFEST_NAME, json.dumps(manifest, indent=2).encode())]
    if not scan.full:
        meta.append((LEFT_OUT_NAME, _left_out_tsv(scan)))
    for name, blob in meta:
        info = tarfile.TarInfo(name)
        info.size = len(blob)
        info.mtime = int(datetime.now(timezone.utc).timestamp())
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(blob))


def _restore_filter(member: tarfile.TarInfo, path: str) -> tarfile.TarInfo:
    """Keep an extraction inside its target without dropping our symlinks.

    tarfile's `data` filter refuses any link that points outside the archive,
    which is exactly what the plugin-delivery links do — they point at the
    installed wheel. So member *names* are policed as strictly as `data` does
    (no absolute paths, no `..`), link *targets* are left alone, and the odd
    permission bits nobody wants restored are cleared.

    Allowing those link targets is only safe because nothing is ever written
    *through* a link: `audit_members` refuses an archive where a member's path
    passes through, or collides with, a symlink the same archive creates.
    """
    name = PurePosixPath(member.name)
    if name.is_absolute() or ".." in name.parts:
        raise ValueError(f"refusing archive member outside the target: {member.name}")
    if member.isdev():
        raise ValueError(f"refusing device node in archive: {member.name}")
    if member.islnk():
        raise ValueError(f"refusing hard link in archive: {member.name}")
    member.mode = member.mode & 0o777 & ~(stat.S_ISUID | stat.S_ISGID)
    return member


def _case_insensitive(directory: Path) -> bool:
    """Whether this filesystem would fold two names into one. APFS does."""
    probe = directory / ".woltspace-Case-Probe"
    try:
        probe.touch()
        return (directory / ".woltspace-case-probe").exists()
    except OSError:
        return False
    finally:
        try:
            probe.unlink()
        except OSError:
            pass


def audit_members(members: list[tarfile.TarInfo], target: Path) -> None:
    """Refuse an archive that could write anywhere but into `target`.

    The whole-archive checks that a per-member filter cannot make, because they
    are about how members relate to each other:

    * a **hard link** never appears in an archive this module writes — real
      colony data does contain them (Claude's `file-history` dedupes versions
      that way) and `_add_entry` materializes each one as a full regular file,
      so a LNK member here came from somewhere else and its target would be
      resolved by the extractor rather than by us;
    * a member whose path passes *through* a symlink this archive creates
      (`x` → /elsewhere, then `x/evil`), or that **replaces** one (`x` → /file,
      then a regular `x`), is the classic write-through-a-link trick;
    * **duplicate names** are how that trick is smuggled past a reader that only
      looks at the last member, so any repeat is refused outright — a backup
      this code wrote never contains one;
    * names that differ **only by case** cannot both exist on APFS, and silently
      keeping one file's content under another's name is exactly the corruption
      a restore must not perform.
    """
    seen: set[str] = set()
    symlinks: set[str] = set()
    folded: dict[str, str] = {}
    for member in members:
        name = member.name.rstrip("/")
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"refusing archive member outside the target: {member.name}")
        if member.islnk():
            raise ValueError(f"refusing hard link in archive: {member.name} → {member.linkname}")
        if member.isdev():
            raise ValueError(f"refusing device node in archive: {member.name}")
        if name in seen:
            raise ValueError(f"refusing duplicate archive member: {member.name}")
        seen.add(name)
        for ancestor in list(path.parents)[:-1]:
            if ancestor.as_posix() in symlinks:
                raise ValueError(
                    f"refusing member whose path runs through a symlink: {member.name}"
                )
        if name in symlinks:
            raise ValueError(f"refusing member that overwrites a symlink: {member.name}")
        if member.issym():
            symlinks.add(name)
        clash = folded.setdefault(name.casefold(), name)
        if clash != name and _case_insensitive(target):
            raise ValueError(
                "refusing archive: this filesystem folds case and these members "
                f"would collide: {clash} / {member.name}"
            )


#: Runtime records the control plane writes about processes it runs. In a copy
#: they name the source's processes: a copy that believed them would adopt the
#: source's tunnel and connectors as its own, and stop them on request. The
#: control plane rewrites all of them when it boots, so a restore removes them.
RUNTIME_RECORDS = (
    ".space/platform/control-plane.json", ".space/platform/control-plane.lock",
    ".space/platform/tunnel.json", ".space/platform/connectors.json",
    ".space/platform/adoption.json",
)
#: Where a restore sets aside session records that point outside the lodge.
QUARANTINE_NAME = "restore-quarantine"
#: Written into a restored copy. While it is there, the platform runs the copy
#: on its own tmux server (`<target>/tmux`), never the machine's shared one: a
#: session's tmux name is its own name, so a copy on the shared server could
#: stop its source's sessions. Delete it to make the copy the machine's lodge.
RESTORED_MARKER = ".space/platform/restored.json"
RESTORED_TMUX_NAME = "tmux"
#: Session statuses a control plane would adopt on boot (by tmux name).
_LIVE_SESSION_STATUSES = ("running", "orphaned", "starting", "pending", "stopping")
_SESSION_PATH_KEYS = ("workdir", "dir")
#: What an app's state says about the processes behind it.
_APP_PID_KEYS = ("pid", "tunnel_pid", "tunnel_url")
#: A pre-lodge.db state file, or the copy lodge.db's importer left of it.
_LEGACY_JSON = re.compile(r".+\.json(\.imported(\.\d+)?)?")


#: Where the container mounts the data root. A lodge that ran in the container
#: era recorded its sessions under this path: it is the same lodge.
CONTAINER_WOLTS_DIR = "/workspace/wolts"


def _rebase(value: str, source: str, wolts_dir: Path) -> str | None:
    """`value` moved from the source lodge into this one, or None if outside it."""
    for root in (source, CONTAINER_WOLTS_DIR):
        if not root:
            continue
        if value == root:
            return str(wolts_dir)
        if value.startswith(root + "/"):
            return str(wolts_dir) + value[len(root):]
    return None


def _rebase_strings(value, source: str, wolts_dir: Path):
    if isinstance(value, str):
        moved = _rebase(value, source, wolts_dir)
        return value if moved is None else moved
    if isinstance(value, list):
        return [_rebase_strings(item, source, wolts_dir) for item in value]
    if isinstance(value, dict):
        return {key: _rebase_strings(item, source, wolts_dir) for key, item in value.items()}
    return value


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n")


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def restored_tmux_dir(wolts_dir: Path) -> Path | None:
    """The private tmux directory of a restored copy, or None for a lodge."""
    if (Path(wolts_dir) / RESTORED_MARKER).is_file():
        return Path(wolts_dir).parent / RESTORED_TMUX_NAME
    return None


def _disarm_path(rel: str) -> bool:
    """Whether a lodge-relative path is one a restore must rewrite or remove."""
    parts = rel.split("/") if rel else []
    if parts[:1] == [".archive"]:
        parts = parts[1:]  # an archived wolt keeps its sessions too
    if len(parts) <= 1:
        return True  # the data root, or anything at its top level
    if parts[0] == ".space":
        name = parts[1]
        return len(parts) <= 3 and (
            name in {"apps", "platform"} or name.startswith(("lodge.db", "auto-grants.json"))
        )
    return parts[1] == ".state" and (
        len(parts) == 2 or (parts[2].startswith("sessions") and len(parts) <= 4)
    )


def _refuse_disarm_links(members: list[tarfile.TarInfo]) -> None:
    """Refuse an archive whose links would carry a restore's writes elsewhere.

    The audit lets a symlink point outside the target, so a link where a
    restore rewrites or deletes runtime records (`.space`, `lodge.db`, a
    wolt's `.state`, the data root itself) would let it rewrite or delete the
    files at the far end: another lodge's. No backup of an ordinary lodge has
    one.
    """
    for member in members:
        name = member.name.rstrip("/")
        if not member.issym() or not (name == ARCHIVE_ROOT or name.startswith(ARCHIVE_ROOT + "/")):
            continue
        rel = name[len(ARCHIVE_ROOT) + 1:]
        link = PurePosixPath(member.linkname)
        if rel and "/" not in rel and not link.is_absolute() and ".." not in link.parts:
            continue  # a top-level link to somewhere inside the lodge is disarmed there
        if _disarm_path(rel):
            raise ValueError(
                f"refusing archive: {name} is a symlink where a restore must disarm "
                "the copy, and it could reach a live lodge"
            )


def _guard(wolts_dir: Path, path: Path) -> Path:
    """Fail closed when a path to rewrite or delete runs through a symlink."""
    here = path
    while True:
        if here.is_symlink():
            raise ValueError(f"refusing to disarm through a symlink: {here}")
        if here == wolts_dir or here.parent == here:
            return path
        here = here.parent


def _wolt_dirs(wolts_dir: Path) -> list[Path]:
    """Every directory that can hold a wolt's `.state`, archived wolts too."""
    found = []
    for parent in (wolts_dir, wolts_dir / ".archive"):
        if parent.is_symlink() or not parent.is_dir():
            continue  # a linked `.archive` is checked below, as a top-level link
        for top in sorted(parent.iterdir()):
            if top.is_symlink():
                if not top.resolve().is_relative_to(wolts_dir.resolve()):
                    raise ValueError(f"refusing to disarm through a symlink: {top}")
                continue  # points inside the lodge: disarmed under its real name
            if top.is_dir():
                found.append(top)
    return found


def _disarm_session(record: dict, source: str, wolts_dir: Path, report: dict) -> tuple[dict, list]:
    """One session, stopped, without its runtime handle, its paths re-pointed.

    Returns the new record and the absolute paths it names outside the lodge.
    """
    workdirs = [record.get(key) for key in _SESSION_PATH_KEYS]
    workdirs.append((record.get("target") or {}).get("canonical_workdir"))
    outside = sorted({
        value for value in workdirs
        if isinstance(value, str) and value.startswith("/")
        and _rebase(value, source, wolts_dir) is None
    })
    record = dict(record)
    if record.get("status") in _LIVE_SESSION_STATUSES:
        record["status"] = "stopped"
        record["finished_at"] = record.get("finished_at") or int(datetime.now(timezone.utc).timestamp())
        report["sessions_stopped"] += 1
    record.pop("runtime", None)  # tmux name and pane belong to the source's tmux
    rebased = _rebase_strings(record, source, wolts_dir)
    if rebased != record:
        report["sessions_rebased"] += 1
    return rebased, outside


def _safe_name(value) -> str:
    """A database value as one path component under the quarantine."""
    name = str(value or "").replace("/", "_").replace("\\", "_")
    return "_" if name in ("", ".", "..") else name


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}


def _disarm_lodge_db(wolts_dir: Path, source: str, target: Path, report: dict) -> None:
    """Disarm the copy's own `lodge.db`, the one under the restore target.

    Sessions, auto-mode approvals and app state live here from 0.5.23 on. A
    session that worked outside the lodge leaves the database for the
    quarantine, written the way a pre-lodge.db release kept it
    (`<wolt>/.state/sessions/<name>.json`): moved back into the copy, the next
    boot imports it. A table without the columns the lodge reads is one no
    lodge acts on, and is left as it is.
    """
    db = _guard(wolts_dir, wolts_dir / LODGE_DB)
    for companion in SQLITE_COMPANIONS:
        _guard(wolts_dir, db.with_name(db.name + companion))
    if not db.is_file():
        return
    with closing(sqlite3.connect(db, isolation_level=None)) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.execute("BEGIN IMMEDIATE")
        if "apps_running" in tables and {"name", "record"} <= _columns(conn, "apps_running"):
            # On the machine a backup lands on, an old pid may be any live
            # process: the lodge would take the app as running, and stopping
            # it would signal that process. Without them, the lodge starts it.
            blank = [key for key in _APP_PID_KEYS if key in _columns(conn, "apps_running")]
            paths = ", ".join(f"'$.{key}'" for key in _APP_PID_KEYS)
            conn.execute(
                "UPDATE apps_running SET "
                + "".join(f"{key}=NULL, " for key in blank)
                + f"record=json_remove(record, {paths})"
            )
            report["apps"] += [row[0] for row in conn.execute("SELECT name FROM apps_running")]
        if "sessions" in tables and {"name", "record"} <= _columns(conn, "sessions"):
            columns = _columns(conn, "sessions")
            for name, raw in conn.execute("SELECT name, record FROM sessions").fetchall():
                try:
                    record = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if not isinstance(record, dict):
                    continue
                rebased, outside = _disarm_session(record, source, wolts_dir, report)
                if outside:
                    rel = f"{_safe_name(rebased.get('wolt'))}/.state/sessions/{_safe_name(name)}.json"
                    aside = target / QUARANTINE_NAME / rel
                    aside.parent.mkdir(parents=True, exist_ok=True)
                    _write_json(aside, {"name": name, **rebased})
                    conn.execute("DELETE FROM sessions WHERE name=?", (name,))
                    report["sessions_set_aside"].append({"path": rel, "outside": outside})
                    continue
                sets = {"record": json.dumps(rebased)}
                for key in ("status", "finished_at"):
                    if key in columns and key in rebased:
                        sets[key] = rebased[key]
                conn.execute(
                    f"UPDATE sessions SET {', '.join(f'{key}=?' for key in sets)} WHERE name=?",
                    (*sets.values(), name),
                )
        if "grants" in tables and {"kind", "wolt_id", "scope"} <= _columns(conn, "grants"):
            rows = conn.execute(
                "SELECT rowid, wolt_id, scope FROM grants WHERE kind='auto'").fetchall()
            for rowid, wolt_id, scope in rows:
                moved = _rebase(scope, source, wolts_dir) if isinstance(scope, str) else None
                if moved is None:
                    conn.execute("DELETE FROM grants WHERE rowid=?", (rowid,))
                    report["grants_dropped"].append(scope)
                    continue  # approval for a folder the copy does not hold: ask again
                # Two approvals that land on one folder (source path and
                # /workspace/wolts) are one approval in the copy.
                conn.execute("UPDATE OR REPLACE grants SET scope=? WHERE rowid=?", (moved, rowid))
                report["grants_rebased"] += 1
        conn.execute("COMMIT")


def _disarm_legacy_files(wolts_dir: Path, source: str, target: Path, report: dict) -> None:
    """The same disarm for the state files a release before lodge.db wrote.

    An older archive holds nothing else, and the copy's first boot imports
    them into its lodge.db. The `.imported` copies an import leaves behind
    are disarmed too: going back to an older release puts them back.
    """
    apps_state = _guard(wolts_dir, wolts_dir / ".space" / "apps")
    if apps_state.is_dir():
        for path in sorted(apps_state.iterdir()):
            if not _LEGACY_JSON.fullmatch(path.name) or not _guard(wolts_dir, path).is_file():
                continue
            state = _read_json(path)
            if isinstance(state, dict):
                _write_json(path, {k: v for k, v in state.items() if k not in _APP_PID_KEYS})
                report["apps"].append(path.name.split(".json", 1)[0])

    for top in _wolt_dirs(wolts_dir):
        state_dir = _guard(wolts_dir, top / ".state")
        if not state_dir.is_dir():
            continue
        for folder in sorted(state_dir.iterdir()):
            if not folder.name.startswith("sessions") or not _guard(wolts_dir, folder).is_dir():
                continue
            for path in sorted(folder.glob("*.json")):
                record = _read_json(_guard(wolts_dir, path)) if path.is_file() else None
                if not isinstance(record, dict):
                    continue
                rebased, outside = _disarm_session(record, source, wolts_dir, report)
                rel = _rel(wolts_dir, path)
                if outside:
                    aside = target / QUARANTINE_NAME / rel
                    aside.parent.mkdir(parents=True, exist_ok=True)
                    _write_json(aside, rebased)
                    path.unlink()
                    report["sessions_set_aside"].append({"path": rel, "outside": outside})
                    continue
                _write_json(path, rebased)

    space = _guard(wolts_dir, wolts_dir / ".space")
    for grants_path in sorted(space.glob("auto-grants.json*")) if space.is_dir() else []:
        if not _LEGACY_JSON.fullmatch(grants_path.name) or not _guard(wolts_dir, grants_path).is_file():
            continue
        grants = _read_json(grants_path)
        if not (isinstance(grants, dict) and isinstance(grants.get("grants"), list)):
            continue
        kept = []
        for grant in grants["grants"]:
            workdir = grant.get("canonical_workdir") if isinstance(grant, dict) else None
            moved = _rebase(workdir, source, wolts_dir) if isinstance(workdir, str) else None
            if moved is None:
                report["grants_dropped"].append(workdir)
                continue  # approval for a folder the copy does not hold: ask again
            kept.append({**grant, "canonical_workdir": moved})
            report["grants_rebased"] += 1
        grants["grants"] = kept
        _write_json(grants_path, grants)


def disarm(wolts_dir: Path, source: str, target: Path) -> dict:
    """Make a restored lodge a copy that cannot reach the lodge it came from.

    A copy restored on the machine that made it shares that machine's process
    table, its tmux server and its paths. Left as archived, it would adopt the
    source's live apps and sessions on boot, and stopping one in the copy would
    stop the original. So the copy's runtime records go, its apps forget their
    pids, its sessions come back stopped with no runtime handle, and every path
    that pointed inside the old lodge is re-pointed at the new one. A session
    that worked outside the lodge is set aside, never re-pointed: its directory
    is not part of the backup. Transcripts and resume ids are untouched, so
    every session can still be resumed in the copy.

    Only the copy is opened: its `lodge.db`, and the older state files beside
    it, all under `wolts_dir`.
    """
    report = {
        "source": source, "runtime_records": [], "apps": [], "sessions_stopped": 0,
        "sessions_rebased": 0, "sessions_set_aside": [], "grants_rebased": 0,
        "grants_dropped": [],
    }
    _guard(wolts_dir, wolts_dir)
    for rel in RUNTIME_RECORDS:
        path = _guard(wolts_dir, wolts_dir / rel)
        if path.is_file():
            path.unlink()
            report["runtime_records"].append(rel)
    _disarm_lodge_db(wolts_dir, source, target, report)
    _disarm_legacy_files(wolts_dir, source, target, report)

    marker = _guard(wolts_dir, wolts_dir / RESTORED_MARKER)
    marker.parent.mkdir(parents=True, exist_ok=True)
    _write_json(marker, {
        "source": source,
        "restored_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tmux": f"runs on its own tmux server in ../{RESTORED_TMUX_NAME}; delete this file "
                "to make this copy the machine's lodge",
    })
    (wolts_dir.parent / RESTORED_TMUX_NAME).mkdir(exist_ok=True)
    return report


def restore_backup(
    archive: Path | str, *, to: Path | str | None = None, identity: bytes | None = None,
) -> RestoreResult:
    """Extract a snapshot into a NEW directory. Never over a populated one.

    An encrypted archive needs the owner's age `identity` (the key file's
    bytes). It is decrypted twice, never to disk: one pass to audit every
    member, then the extraction pass, which must see the same members.

    There is no `--force`. Restoring is half the job; pointing a live colony at
    the result is the other half, and that half stays a human's explicit act.
    """
    archive = Path(archive).expanduser().resolve(strict=False)
    if not archive.is_file():
        raise FileNotFoundError(f"no archive at {archive}")
    encrypted = archive.name.endswith(".age")
    if encrypted and not identity:
        raise ValueError(f"{archive.name} is encrypted: pass --identity <age key file> (or - for stdin)")
    stem = archive.name
    for suffix in (".tar.gz.age", ".tar.gz", ".tgz"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    target = Path(to).expanduser().resolve(strict=False) if to else archive.parent / f"{stem}-restored"
    if target.exists() and any(target.iterdir()):
        raise FileExistsError(f"{target} exists and is not empty — restore wants a fresh directory")
    target.mkdir(parents=True, exist_ok=True)

    if encrypted:
        with _decrypted(archive, identity) as stream, tarfile.open(fileobj=stream, mode="r|gz") as tar:
            members = list(tar)
        # Audit the whole member list before a single byte lands: a refusal
        # after a partial extraction is a refusal that already happened.
        audit_members(members, target)
        _refuse_disarm_links(members)
        with _decrypted(archive, identity) as stream, tarfile.open(fileobj=stream, mode="r|gz") as tar:
            tar.extractall(target, members=_same_members(tar, members), filter=_restore_filter)
    else:
        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
            audit_members(members, target)
            _refuse_disarm_links(members)
            tar.extractall(target, members=members, filter=_restore_filter)
    manifest = json.loads((target / MANIFEST_NAME).read_text())
    wolts_dir = target / manifest.get("archive_root", ARCHIVE_ROOT)
    disarmed = disarm(wolts_dir, manifest.get("source") or "", target) if wolts_dir.is_dir() else {}
    return RestoreResult(
        target=target,
        wolts_dir=wolts_dir,
        manifest=manifest,
        entries=len([m for m in members if m.name not in _META_NAMES]),
        disarmed=disarmed,
    )


@contextmanager
def _decrypted(archive: Path, identity: bytes):
    """`age -d` of `archive` as a stream; the key goes in on stdin, never to disk."""
    age = shutil.which("age")
    if not age:
        raise OSError("age is not on PATH — it is needed to open an encrypted backup")
    proc = subprocess.Popen(
        [age, "-d", "-i", "-", str(archive)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    proc.stdin.write(identity)
    proc.stdin.close()
    try:
        yield proc.stdout
        proc.stdout.read()  # the tar's trailing padding, so age can finish
    except BaseException as exc:
        proc.kill()
        error = proc.stderr.read().decode(errors="replace").strip()
        proc.wait()
        if error:
            raise OSError(f"age could not decrypt {archive.name}: {error}") from exc
        raise
    error = proc.stderr.read().decode(errors="replace").strip()
    if proc.wait() != 0:
        raise OSError(f"age could not decrypt {archive.name}: {error}")


def _same_members(tar: tarfile.TarFile, audited: list[tarfile.TarInfo]):
    """Yield the extraction pass's members, refusing any the audit did not see."""
    def shape(member: tarfile.TarInfo) -> tuple:
        return member.name, member.type, member.size, member.linkname

    count = 0
    for member in tar:
        if count >= len(audited) or shape(member) != shape(audited[count]):
            raise ValueError("the archive changed between the audit and the extraction")
        count += 1
        yield member
    if count != len(audited):
        raise ValueError("the archive changed between the audit and the extraction")


def human_bytes(count: int) -> str:
    value = float(count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}TB"


def summary_lines(result: BackupResult) -> list[str]:
    """The completion report — sizes before and after, so the win is visible."""
    totals = result.manifest["totals"]
    lines = [
        f"backup: {result.archive}",
        f"tag: {result.manifest['tag']}  ·  woltspace {result.manifest['woltspace_version']}",
        f"source: {result.manifest['source']}",
        (
            f"data: {human_bytes(totals['included_bytes'])} in "
            f"{totals['included_files']} files "
            f"(scanned {human_bytes(totals['scanned_bytes'])}, "
            f"excluded {human_bytes(totals['excluded_bytes'])} "
            f"in {totals['excluded_files']} files)"
        ),
        f"archive: {human_bytes(result.archive.stat().st_size)} · {result.manifest['entries']} entries",
    ]
    lines.extend(_left_out_lines(result.manifest))
    for wolt in result.manifest["wolts"]:
        lines.append(
            f"  wolt {wolt['name']}: {human_bytes(wolt['bytes'])} in {wolt['files']} files"
            + (f" (−{human_bytes(wolt['excluded_bytes'])} excluded)" if wolt["excluded_bytes"] else "")
        )
    withheld = result.manifest["withheld"]["paths"]
    secrets = result.manifest["secrets"]
    if withheld:
        # Encrypted, the .env files and bot tokens travel inside, except a symlinked
        # one keep_secrets skipped; the rest stays out.
        skipped = {item["path"] for item in result.manifest["unreadable"]}
        inside = sum(
            item["rule"] in ENCRYPTED_RULES and item["path"] not in skipped for item in withheld
        ) if secrets["present"] else 0
        where = [f"{count} {text}" for count, text in (
            (len(withheld) - inside, "not in the archive"), (inside, "inside, encrypted"),
        ) if count]
        lines.append(
            f"withheld: {len(withheld)} credential file(s) — {', '.join(where)}, "
            f"listed in {MANIFEST_NAME}"
        )
    if secrets["present"]:
        lines.append(f"encrypted to {secrets['recipient']}, .env files and bot tokens inside")
    elif any(item["rule"] in ENCRYPTED_RULES for item in withheld):
        lines.append(f"secrets: not kept — {secrets['reason']}")
    unreadable = result.manifest["unreadable"]
    if unreadable:
        lines.append(f"warnings: {len(unreadable)} path(s) skipped")
        for item in unreadable[:5]:
            lines.append(f"  {item['path']}: {item['error']}")
        if len(unreadable) > 5:
            lines.append(f"  … {len(unreadable) - 5} more (see {MANIFEST_NAME} in the archive)")
    if secrets["present"]:
        lines.append("verified: age wrote it; only a restore with the key opens it")
    else:
        lines.append("verified: manifest parses, entry count matches, wolt.json readable")
    return lines


#: What to do about each kind of withheld file, said in the imperative. A
#: restore that is quietly missing credentials is a restore that fails later,
#: somewhere confusing.
REPROVISION = {
    "dotenv": "recreate it — copy .env.example and fill in the tokens",
    "claude-credentials": "re-authenticate Claude: `woltspace init`, or `claude login`",
    "codex-auth": "re-authenticate codex: `codex login`",
    "platform-config": "re-provision config.json (bot tokens) — reconnect the channels",
}


def reprovision_lines(manifest: dict) -> list[str]:
    """The checklist a human works through after a restore."""
    withheld = (manifest.get("withheld") or {}).get("paths") or []
    if (manifest.get("secrets") or {}).get("present"):
        skipped = {item["path"] for item in manifest.get("unreadable") or []}
        withheld = [
            item for item in withheld
            if item["rule"] not in ENCRYPTED_RULES or item["path"] in skipped
        ]
    if not withheld:
        return []
    by_rule: dict[str, list[str]] = {}
    for item in withheld:
        by_rule.setdefault(item["rule"], []).append(item["path"])
    lines = [
        "",
        f"withheld from this backup — {len(withheld)} credential file(s), by design:",
    ]
    for rule, paths in sorted(by_rule.items()):
        lines.append(f"  {rule}: {REPROVISION.get(rule, 'restore this credential by hand')}")
        for path in sorted(paths):
            lines.append(f"    {path}")
    lines.append("  (a stale rotating token, restored, can revoke the live one)")
    return lines


def _left_out_lines(manifest: dict) -> list[str]:
    """Say what a slim backup kept out, or that it kept everything."""
    if manifest.get("mode") != "slim":
        return ["mode: full — media and every other file kept"] if manifest.get("mode") else []
    left = manifest["left_out"]
    return [
        "mode: slim — all context and code kept",
        (
            f"left out: {human_bytes(left['bytes'])} in {left['files']} files "
            f"(media, binaries, databases, caches, checkouts), listed in {left['listed_in']}; "
            "`--full` keeps them"
        ),
    ]


def restore_lines(result: RestoreResult) -> list[str]:
    manifest = result.manifest
    lines = [
        f"restored: {result.target}",
        f"tag: {manifest['tag']}  ·  taken {manifest['created_at']} on {manifest['host']}",
        f"woltspace {manifest['woltspace_version']}  ·  {result.entries} entries",
        f"wolts: {', '.join(wolt['name'] for wolt in manifest['wolts']) or '(none)'}",
    ]
    if manifest.get("mode") == "slim":
        left = manifest["left_out"]
        lines.append(
            f"left out: {left['files']} files ({human_bytes(left['bytes'])}) were not in this "
            f"slim backup — named in {left['listed_in']}"
        )
    lines.extend(disarm_lines(result))
    lines.extend(reprovision_lines(manifest))
    if "lodge_db" in manifest:
        lines.extend([
            "",
            "consents came back with the lodge: review them with woltspace auto list /",
            "  woltspace dig list and revoke what this machine should not have",
        ])
    lines.extend([
        "",
        "boot from it with (beside a running lodge, give it its own port and no tunnel):",
        (
            f"  WOLTSPACE_PORT=7788 WOLTSPACE_PUBLIC_TUNNEL=false "
            f"WOLTSPACE_WOLTS_DIR={result.wolts_dir} woltspace start"
        ),
        f"it runs on its own tmux server ({result.target / RESTORED_TMUX_NAME}) until you delete",
        f"  {result.wolts_dir / RESTORED_MARKER}",
    ])
    return lines


def disarm_lines(result: RestoreResult) -> list[str]:
    """Say what the restore changed so the copy keeps its hands off the source."""
    done = result.disarmed
    if not done:
        return []
    lines = [
        f"disarmed: {done['sessions_stopped']} session(s) come back stopped and "
        f"{len(done['apps'])} app(s) without their pids; nothing from the source's runtime "
        "is adopted",
    ]
    if done["sessions_rebased"] or done["grants_rebased"]:
        lines.append(
            f"re-pointed: {done['sessions_rebased']} session(s) and {done['grants_rebased']} "
            f"auto-mode approval(s) from {done['source']} to this copy"
        )
    if done["sessions_set_aside"]:
        lines.append(
            f"set aside: {len(done['sessions_set_aside'])} session(s) that worked outside the "
            f"lodge, in {result.target / QUARANTINE_NAME} (transcripts kept)"
        )
    if done["grants_dropped"]:
        lines.append(
            f"approvals dropped: {len(done['grants_dropped'])} for folders outside the lodge; "
            "auto mode asks again"
        )
    return lines
