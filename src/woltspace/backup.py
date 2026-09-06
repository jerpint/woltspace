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
)

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
)


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
    return None

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
    withheld: list[dict] = field(default_factory=list)
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
    if is_wolt_dir(path):
        return False  # a wolt is never junk, wherever it happens to sit
    if "/" not in rel:  # a direct child of the data root
        return name in ROOT_EXCLUDED_DIR_NAMES or _path_excluded(rel)
    return name in EXCLUDED_DIR_NAMES or _path_excluded(rel)


def _file_excluded(name: str, rel: str) -> bool:
    if name in EXCLUDED_FILE_NAMES or name.endswith(EXCLUDED_FILE_SUFFIXES):
        return True
    return _path_excluded(rel)


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
        if child.is_dir() and not child.is_symlink() and is_wolt_dir(child):
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
            if _dir_excluded(name, rel, child):
                size, files, has_wolt = _probe_excluded(child)
                if has_wolt:
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
                _add_entry(tar, entry)
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
        # Audit the whole member list before a single byte lands: a refusal
        # after a partial extraction is a refusal that already happened.
        audit_members(members, target)
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
    withheld = result.manifest["withheld"]["paths"]
    if withheld:
        lines.append(
            f"withheld: {len(withheld)} credential file(s) — not in the archive, "
            f"listed in {MANIFEST_NAME}"
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


#: What to do about each kind of withheld file, said in the imperative. A
#: restore that is quietly missing credentials is a restore that fails later,
#: somewhere confusing.
REPROVISION = {
    "dotenv": "recreate it — copy .env.example and fill in the tokens",
    "claude-credentials": "re-authenticate Claude: `woltspace init`, or `claude login`",
    "codex-auth": "re-authenticate codex: `codex login`",
}


def reprovision_lines(manifest: dict) -> list[str]:
    """The checklist a human works through after a restore."""
    withheld = (manifest.get("withheld") or {}).get("paths") or []
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


def restore_lines(result: RestoreResult) -> list[str]:
    manifest = result.manifest
    lines = [
        f"restored: {result.target}",
        f"tag: {manifest['tag']}  ·  taken {manifest['created_at']} on {manifest['host']}",
        f"woltspace {manifest['woltspace_version']}  ·  {result.entries} entries",
        f"wolts: {', '.join(wolt['name'] for wolt in manifest['wolts']) or '(none)'}",
    ]
    lines.extend(reprovision_lines(manifest))
    lines.extend([
        "",
        "boot from it with:",
        f"  WOLTS_DIR={result.wolts_dir} woltspace start",
    ])
    return lines
