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

Nothing here touches docker, takes a lock, or reads a credential's value. A
backup of a live colony is safe; a quiescent one only gives a tidier registry.
"""

from __future__ import annotations

import io
import json
import os
import socket
import stat
import tarfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from . import __version__

#: Everything inside the archive lives under this prefix, so a restore lands a
#: single directory the human can point WOLTS_DIR at, with the manifest beside
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
})

#: Paths excluded by their tail rather than by a bare name — a directory called
#: `cache` is only junk when it is Claude's plugin cache, and `worktrees` only
#: when it is a wolt's pile of throwaway checkouts.
EXCLUDED_PATH_SUFFIXES = (
    ".claude/plugins/cache",
    "wolt/worktrees",
)

EXCLUDED_FILE_NAMES = frozenset({".DS_Store"})
EXCLUDED_FILE_SUFFIXES = (".pyc", ".sock")

#: `.git` is deliberately absent from all of the above: a wolt's history is
#: data, and a snapshot that loses it is not a snapshot.


def default_tag() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def archive_name(tag: str) -> str:
    return f"woltspace-backup-{tag}.tar.gz"


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
    unreadable: list[dict] = field(default_factory=list)
    wolts: list[dict] = field(default_factory=list)

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


def _rel(root: Path, path: Path) -> str:
    return PurePosixPath(os.path.relpath(path, root)).as_posix()


def _path_excluded(rel: str) -> bool:
    return any(rel == suffix or rel.endswith("/" + suffix) for suffix in EXCLUDED_PATH_SUFFIXES)


def _dir_excluded(name: str, rel: str) -> bool:
    return name in EXCLUDED_DIR_NAMES or _path_excluded(rel)


def _file_excluded(name: str, rel: str) -> bool:
    if name in EXCLUDED_FILE_NAMES or name.endswith(EXCLUDED_FILE_SUFFIXES):
        return True
    return _path_excluded(rel)


def _tree_bytes(path: Path) -> tuple[int, int]:
    """Size an excluded subtree, so the summary can name what it left behind."""
    total = 0
    count = 0
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        for name in list(dirnames):
            if Path(dirpath, name).is_symlink():
                dirnames.remove(name)
        for name in filenames:
            try:
                info = os.lstat(Path(dirpath, name))
            except OSError:
                continue
            count += 1
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
    return total, count


def scan_tree(wolts_dir: Path) -> Scan:
    """Walk the data root once, deciding membership before anything is written.

    Symlinks are never followed. The plugin-delivery links point into the
    installed wheel, and a backup that chased them would swallow the platform
    it is not supposed to be backing up — they are archived as links, and a
    restore recreates links.
    """
    wolts_dir = Path(wolts_dir)
    scan = Scan()
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
        if child.is_dir() and not child.is_symlink() and (child / "wolt").is_dir():
            per_wolt[child.name] = {
                "name": child.name, "bytes": 0, "files": 0, "excluded_bytes": 0,
            }

    def on_error(exc: OSError) -> None:
        scan.unreadable.append({
            "path": _rel(wolts_dir, Path(getattr(exc, "filename", "") or wolts_dir)),
            "error": exc.strerror or str(exc),
        })

    for dirpath, dirnames, filenames in os.walk(wolts_dir, followlinks=False, onerror=on_error):
        here = Path(dirpath)
        if here != wolts_dir:
            scan.entries.append(Entry(here, f"{ARCHIVE_ROOT}/{_rel(wolts_dir, here)}", "dir"))

        for name in sorted(dirnames):
            child = here / name
            rel = _rel(wolts_dir, child)
            if child.is_symlink():
                # A symlinked directory is a link, not a subtree: archive the
                # link itself and never descend through it.
                dirnames.remove(name)
                scan.entries.append(Entry(child, f"{ARCHIVE_ROOT}/{rel}", "symlink"))
                continue
            if _dir_excluded(name, rel):
                dirnames.remove(name)
                size, files = _tree_bytes(child)
                scan.excluded_bytes += size
                scan.excluded_files += files
                note(rel, size, files, included=False)
        dirnames[:] = [name for name in sorted(dirnames)]

        for name in sorted(filenames):
            child = here / name
            rel = _rel(wolts_dir, child)
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
            if not os.access(child, os.R_OK):
                scan.unreadable.append({"path": rel, "error": "unreadable"})
                continue
            scan.entries.append(Entry(child, f"{ARCHIVE_ROOT}/{rel}", "file", info.st_size))
            scan.included_bytes += info.st_size
            scan.included_files += 1
            note(rel, info.st_size, 1, included=True)

    scan.wolts = [per_wolt[name] for name in sorted(per_wolt)]
    return scan


def build_manifest(wolts_dir: Path, tag: str, scan: Scan) -> dict:
    """Describe the snapshot without ever reading a secret's value.

    `.env` is in the archive — that is the point of a backup — but only its
    name and size appear here, and nothing in this function opens a file.
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
        "totals": {
            "scanned_bytes": scan.scanned_bytes,
            "included_bytes": scan.included_bytes,
            "included_files": scan.included_files,
            "excluded_bytes": scan.excluded_bytes,
            "excluded_files": scan.excluded_files,
        },
        "excludes": {
            "dir_names": sorted(EXCLUDED_DIR_NAMES),
            "path_suffixes": list(EXCLUDED_PATH_SUFFIXES),
            "file_names": sorted(EXCLUDED_FILE_NAMES),
            "file_suffixes": list(EXCLUDED_FILE_SUFFIXES),
            "symlinks": "archived as links; never followed",
            "git": "included — history is data",
        },
        "unreadable": scan.unreadable,
    }


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
        counted = len([name for name in names if name != MANIFEST_NAME])
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


def create_backup(
    wolts_dir: Path | str, *, out_dir: Path | str | None = None, tag: str | None = None,
) -> BackupResult:
    """Write `woltspace-backup-<tag>.tar.gz` and verify it before returning."""
    wolts_dir = Path(wolts_dir).expanduser().resolve(strict=False)
    if not wolts_dir.is_dir():
        raise FileNotFoundError(f"no wolts directory at {wolts_dir}")
    tag = tag or default_tag()
    out = Path(out_dir).expanduser().resolve(strict=False) if out_dir else wolts_dir.parent
    out.mkdir(parents=True, exist_ok=True)
    archive = out / archive_name(tag)

    scan = scan_tree(wolts_dir)
    manifest = build_manifest(wolts_dir, tag, scan)

    with tarfile.open(archive, "w:gz") as tar:
        for entry in scan.entries:
            try:
                tar.add(entry.path, arcname=entry.arcname, recursive=False)
            except OSError as exc:
                # A file that vanished or turned unreadable between the scan
                # and the write is a warning, never a failed backup.
                manifest["unreadable"].append({
                    "path": entry.arcname[len(ARCHIVE_ROOT) + 1:],
                    "error": exc.strerror or str(exc),
                })
                manifest["entries"] -= 1
        blob = json.dumps(manifest, indent=2).encode()
        info = tarfile.TarInfo(MANIFEST_NAME)
        info.size = len(blob)
        info.mtime = int(datetime.now(timezone.utc).timestamp())
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(blob))

    verify_archive(archive)
    return BackupResult(archive=archive, manifest=manifest, verified=True, scan=scan)


def _restore_filter(member: tarfile.TarInfo, path: str) -> tarfile.TarInfo:
    """Keep an extraction inside its target without dropping our symlinks.

    tarfile's `data` filter refuses any link that points outside the archive,
    which is exactly what the plugin-delivery links do — they point at the
    installed wheel. So member *names* are policed as strictly as `data` does
    (no absolute paths, no `..`), link targets are left alone, and the odd
    permission bits nobody wants restored are cleared.
    """
    name = PurePosixPath(member.name)
    if name.is_absolute() or ".." in name.parts:
        raise ValueError(f"refusing archive member outside the target: {member.name}")
    if member.isdev():
        raise ValueError(f"refusing device node in archive: {member.name}")
    member.mode = member.mode & 0o777 & ~(stat.S_ISUID | stat.S_ISGID)
    return member


def restore_backup(archive: Path | str, *, to: Path | str | None = None) -> RestoreResult:
    """Extract a snapshot into a NEW directory. Never over a populated one.

    There is no `--force`. Restoring is half the job; pointing a live colony at
    the result is the other half, and that half stays a human's explicit act.
    """
    archive = Path(archive).expanduser().resolve(strict=False)
    if not archive.is_file():
        raise FileNotFoundError(f"no archive at {archive}")
    stem = archive.name
    for suffix in (".tar.gz", ".tgz"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    target = Path(to).expanduser().resolve(strict=False) if to else archive.parent / f"{stem}-restored"
    if target.exists() and any(target.iterdir()):
        raise FileExistsError(f"{target} exists and is not empty — restore wants a fresh directory")
    target.mkdir(parents=True, exist_ok=True)

    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        tar.extractall(target, members=members, filter=_restore_filter)
    manifest = json.loads((target / MANIFEST_NAME).read_text())
    return RestoreResult(
        target=target,
        wolts_dir=target / manifest.get("archive_root", ARCHIVE_ROOT),
        manifest=manifest,
        entries=len([m for m in members if m.name != MANIFEST_NAME]),
    )


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
    for wolt in result.manifest["wolts"]:
        lines.append(
            f"  wolt {wolt['name']}: {human_bytes(wolt['bytes'])} in {wolt['files']} files"
            + (f" (−{human_bytes(wolt['excluded_bytes'])} excluded)" if wolt["excluded_bytes"] else "")
        )
    unreadable = result.manifest["unreadable"]
    if unreadable:
        lines.append(f"warnings: {len(unreadable)} path(s) skipped")
        for item in unreadable[:5]:
            lines.append(f"  {item['path']}: {item['error']}")
        if len(unreadable) > 5:
            lines.append(f"  … {len(unreadable) - 5} more (see {MANIFEST_NAME} in the archive)")
    lines.append("verified: manifest parses, entry count matches, wolt.json readable")
    return lines


def restore_lines(result: RestoreResult) -> list[str]:
    manifest = result.manifest
    lines = [
        f"restored: {result.target}",
        f"tag: {manifest['tag']}  ·  taken {manifest['created_at']} on {manifest['host']}",
        f"woltspace {manifest['woltspace_version']}  ·  {result.entries} entries",
        f"wolts: {', '.join(wolt['name'] for wolt in manifest['wolts']) or '(none)'}",
        "",
        "boot from it with:",
        f"  WOLTS_DIR={result.wolts_dir} woltspace start",
    ]
    return lines
