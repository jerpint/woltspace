"""The session archive: every engine session file, kept for good, under
<data root>/.space/session-archive/ (format v1).

Opt-in: only a lodge whose woltspace.json sets "sessions": {"keep_days": ...}
archives. Each harness entry's "session_files" names where its sessions live,
under every wolt and, natively, under the owner's home; an engine whose store
is a database ("session_store") is reported, not archived. Natively a file is
taken only when the working folder recorded inside it is under the data root;
a file that does not say is skipped and reported, never guessed.

Each source (its origin, "root" or "home", and its relative path) gets an opaque
folder, the sha256 of that pair. In it every part is <source mtime UTC>-<sha256
of the whole content up to it>.gz (gzip, so the archive is kept compressed),
beside a .json naming the original path, capture time, digest and
length, and the digest of the whole content it continues. A JSONL file that only
grew stores just the complete lines added since its last part; anything else is
a whole new version, so an old version is never lost. Names come from content,
so a re-run never stores anything twice: every file is read and hashed each run,
and the parts' own metadata is the only catalog. Nothing in the archive is ever
deleted here.
"""

from __future__ import annotations

import fcntl
import gzip
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import time
import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from .backup import AGE_RECIPIENT_ENV, _encryption, sealed_file, secret_rule
from .backup import SESSION_ARCHIVE as ARCHIVE

FORMAT = "v1"


def enabled(wolts_dir: Path) -> bool:
    try:
        sessions = json.loads((Path(wolts_dir) / "woltspace.json").read_text()).get("sessions")
    except (OSError, ValueError, AttributeError):
        return False
    return isinstance(sessions, dict) and "keep_days" in sessions


@dataclass
class Report:
    stored: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # "path: why"; any makes the command fail
    not_archived: list[str] = field(default_factory=list)  # engines whose store is a database


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(path: Path, data: bytes) -> None:
    """0600 through a temporary file and one rename."""
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.replace(tmp, path)


def _read(path: Path) -> tuple[bytes, os.stat_result]:
    """One read of a regular file, never through a link: the digest names these bytes."""
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise OSError("not a regular file")
        return handle.read(), info


def _inner_cwd(data: bytes) -> str | None:
    for line in data.split(b"\n", 50)[:50]:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        for holder in (record, record.get("payload") if isinstance(record, dict) else None):
            if isinstance(holder, dict) and isinstance(holder.get("cwd"), str):
                return holder["cwd"]
    return None


def _sources(wolts_dir: Path, table: dict, home: Path | None, report: Report):
    """(origin, relative path, path) of every session file: each wolt's own, and
    with a home, the owner's (natively)."""
    bases = [("root", wolts_dir, [child for child in sorted(wolts_dir.iterdir())
                                  if child.is_dir() and not child.name.startswith(".")])]
    if home is not None:
        bases.append(("home", home, [home]))
    for name, entry in table.items():
        if entry.get("session_store"):
            report.not_archived.append(f"{name} ({entry['session_store']}): snapshots only")
        for origin, base, owners in bases:
            for owner in owners:
                for prefix in entry.get("session_files", ()):
                    top = owner / prefix
                    if top.exists() and os.path.realpath(top) != os.path.join(
                            os.path.realpath(base), top.relative_to(base)):
                        report.skipped.append(f"{origin}:{top.relative_to(base).as_posix()}: reached through a link; not followed")
                        continue
                    for dirpath, dirnames, filenames in os.walk(top):
                        dirnames.sort()
                        for filename in sorted(filenames):
                            path = Path(dirpath) / filename
                            yield origin, path.relative_to(base).as_posix(), path


_KEYS = ("origin", "path", "sha256", "length", "offset", "continues", "total_sha256", "total_length",
         "source_mtime_ns")


def _owner_cwd(path: Path, home: Path, seen: dict) -> str | None:
    """The working folder of the session file that owns a file recording none
    (claude's <id>/tool-results/... belongs to the <id>.jsonl beside <id>/)."""
    for folder in path.parents:
        if folder == home or home not in folder.parents:
            return None
        owner = folder.parent / f"{folder.name}.jsonl"
        if owner.is_file():
            if owner not in seen:
                try:
                    seen[owner] = _inner_cwd(_read(owner)[0])
                except OSError:
                    seen[owner] = None
            return seen[owner]
    return None


def _parts(folder: Path, *, strict: bool = False) -> list[dict]:
    """The parts' metadata, oldest first. Capture skips a damaged one (it then
    stores a new version); restore (strict) refuses the source instead, as it
    does a part whose metadata is missing: never an older prefix as complete."""
    metas = []
    for meta in folder.glob("*.json"):
        try:
            record = json.loads(meta.read_text())
            if not isinstance(record, dict) or any(key not in record for key in _KEYS):
                raise ValueError("not a part's metadata")
            metas.append({**record, "name": meta.stem})
        except (OSError, ValueError):
            if strict:
                raise BrokenChain(f"{folder.name}: metadata {meta.name} is damaged") from None
    if strict and (unpaired := sorted(p.name for p in folder.iterdir() if not p.name.startswith(".")
                                       and p.suffix == ".gz" and not (folder / f"{p.stem}.json").exists())):
        raise BrokenChain(f"{folder.name}: part {unpaired[0]} has no metadata")
    return sorted(metas, key=lambda m: (m.get("captured_at", ""), m["name"]))


def _capture(folder: Path, origin: str, rel: str, data: bytes, info: os.stat_result) -> str | None:
    jsonl = rel.endswith(".jsonl")
    body = data[: data.rfind(b"\n") + 1] if jsonl else data  # a half-written line waits
    if not body:
        return None
    total, parts = _sha(body), _parts(folder)
    last = parts[-1] if parts else None
    if last and last.get("total_sha256") == total:
        return None
    start, continues = 0, None
    if jsonl and last and last["total_length"] < len(body) and _sha(body[: last["total_length"]]) == last["total_sha256"]:
        start, continues = last["total_length"], last["total_sha256"]
    part = body[start:]
    mtime = datetime.fromtimestamp(info.st_mtime, UTC).strftime("%Y%m%dT%H%M%SZ")
    name = f"{mtime}-{total}"  # the whole content so far: two equal appends never share a name
    if any(meta["name"] == name for meta in parts):
        return None  # already kept, whole; a damaged copy of it is written again below
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write(folder / f"{name}.gz", gzip.compress(part, mtime=0))  # kept compressed; digests are of the plain bytes
    _write(folder / f"{name}.json", json.dumps({  # written last: a part without it does not count
        "format": FORMAT, "origin": origin, "path": rel, "content": "jsonl" if jsonl else "file",
        "captured_at": datetime.now(UTC).isoformat(timespec="microseconds"),
        "source_mtime_ns": info.st_mtime_ns, "sha256": _sha(part), "length": len(part),
        "offset": start, "continues": continues,
        "total_sha256": total, "total_length": len(body),
    }, indent=1).encode())
    return name


def refresh(wolts_dir: Path, table: dict, *, home: Path | None = None) -> Report:
    """Store every session file's new part; home: the owner's, on a native lodge."""
    wolts_dir = Path(wolts_dir).resolve()
    archive, report = wolts_dir / ARCHIVE, Report()
    (archive / FORMAT).mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(archive, 0o700)
    with os.fdopen(os.open(archive / ".lock", os.O_WRONLY | os.O_CREAT, 0o600), "w") as lock:  # one refresh at a time
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        owners: dict = {}  # an owning session file's working folder, read once per run
        for origin, rel, path in _sources(wolts_dir, table, home, report):
            key = f"{origin}:{rel}"
            if secret_rule(rel):
                report.skipped.append(f"{key}: looks like a credential; not taken")
                continue
            try:
                data, info = _read(path)
            except OSError as exc:
                report.skipped.append(f"{origin}:{rel}: {exc.strerror or exc}")
                continue
            if origin == "home":
                cwd = _inner_cwd(data)
                if cwd is None and home is not None:  # a tool result, say: its session decides
                    cwd = _owner_cwd(path, home, owners)
                if cwd is None or not os.path.isabs(cwd):
                    report.skipped.append(f"{origin}:{rel}: no absolute working folder recorded inside; not taken")
                    continue
                inside = os.path.realpath(cwd)
                if inside != str(wolts_dir) and not inside.startswith(str(wolts_dir) + os.sep):
                    continue  # the owner's own session, not the lodge's
            folder = archive / FORMAT / _sha(key.encode())
            if name := _capture(folder, origin, rel, data, info):
                report.stored.append(f"{key} -> {name}")
    return report


class BrokenChain(ValueError):
    pass


def _join(folder: Path, parts: list[dict], head: dict) -> bytes:
    """One version of a source, from its last part (head) back to a whole one;
    a missing or damaged part stops it: never a shorter file."""
    by_total = {meta["total_sha256"]: meta for meta in parts}
    chain = [head]
    while chain[-1]["continues"]:
        if chain[-1]["continues"] not in by_total:
            raise BrokenChain(f"{chain[-1]['path']}: the part before {chain[-1]['name']} is missing")
        chain.append(by_total[chain[-1]["continues"]])
    data = b""
    for meta in reversed(chain):
        try:
            part = gzip.decompress((folder / f"{meta['name']}.gz").read_bytes())
        except (OSError, EOFError, zlib.error):  # missing, not gzip, cut short, bad deflate
            raise BrokenChain(f"{meta['path']}: part {meta['name']} is missing or damaged") from None
        if _sha(part) != meta["sha256"] or len(data) != meta["offset"] or (
                meta["continues"] and _sha(data) != meta["continues"]):
            raise BrokenChain(f"{meta['path']}: part {meta['name']} is damaged")
        data += part
    return data


def restore(archive: Path, to: Path) -> tuple[list[str], list[str]]:
    """Every source's newest version as a plain file under to/<origin>/<path>,
    with its original mtime, and each earlier version that a rewrite left
    (a chain nothing continues) in a tree of its own, to/versions/<origin>/
    <path>.v1, .v2, ... oldest first, so it never meets a real file.
    Inert: nothing is resumed. (restored, refused)."""
    to = Path(to)
    if to.exists() and any(to.iterdir()):
        raise FileExistsError(f"{to} is not empty")
    restored, refused = [], []
    for folder in sorted(p for p in (Path(archive) / FORMAT).iterdir() if p.is_dir()):
        try:
            parts = _parts(folder, strict=True)
        except BrokenChain as exc:
            refused.append(str(exc))
            continue
        if not parts:
            refused.append(f"{folder.name}: no readable part")
            continue
        meta, continued = parts[-1], {m["continues"] for m in parts if m["continues"]}
        try:
            data = _join(folder, parts, meta)
        except BrokenChain as exc:
            refused.append(str(exc))
            continue
        rel = PurePosixPath(meta["path"])
        if meta["origin"] not in ("root", "home") or rel.is_absolute() or ".." in rel.parts:
            refused.append(f"{folder.name}: names a path outside the restore ({meta['path']})")
            continue
        target = to / meta["origin"] / rel
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _write(target, data)
        os.utime(target, ns=(time.time_ns(), meta["source_mtime_ns"]))
        restored.append(f"{meta['origin']}:{meta['path']}")
        older = [m for m in parts[:-1] if m["total_sha256"] not in continued]
        for number, head in enumerate(older, 1):
            try:
                earlier = _join(folder, parts, head)
            except BrokenChain as exc:
                refused.append(f"{exc} (earlier version {number})")
                continue
            version = to / "versions" / meta["origin"] / f"{rel}.v{number}"
            version.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _write(version, earlier)
            os.utime(version, ns=(time.time_ns(), head["source_mtime_ns"]))
            restored.append(f"versions/{meta['origin']}:{meta['path']}.v{number}")
    return restored, refused


SENT = "sent.txt"  # parts already confirmed uploaded (see mark_sent)


def _sent(archive: Path) -> set[str]:
    try:
        return set((archive / SENT).read_text().split())
    except OSError:
        return set()


def write_out(wolts_dir: Path, out: Path) -> list[str]:
    """Every archived part not yet sent, age-encrypted to the snapshot key as it
    is kept (already gzipped): <format>/<source id>/<part>.gz.age plus its
    .json.age, so decrypting each file gives back the archive's own layout.
    The names hold no path (inside the encrypted metadata only) and stay under
    200 bytes. Files already in out are left alone; nothing counts as sent until
    mark_sent says the upload worked."""
    encryption = _encryption()
    if encryption is None:
        raise ValueError(f"{AGE_RECIPIENT_ENV} is not set: session parts are never written out in plaintext")
    archive, written = Path(wolts_dir) / ARCHIVE, []
    sent = _sent(archive)
    for left in sorted((Path(out) / FORMAT).glob("*/*")):  # out is ours: nothing stale may be uploaded
        if left.name.startswith(".") or not (archive / FORMAT / left.parent.name / left.name.removesuffix(".age")).is_file():
            left.unlink()  # a run killed mid-write, or a part since deleted on purpose
    for meta in sorted((archive / FORMAT).glob("*/*.json")):
        part = f"{meta.parent.name}/{meta.stem}"
        if part in sent:
            continue
        for source in (meta.with_suffix(".gz"), meta):
            target = Path(out) / FORMAT / meta.parent.name / f"{source.name}.age"
            if target.exists():
                continue
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with sealed_file(target, encryption) as sink:
                sink.write(source.read_bytes())
        written.append(part)
    return written


def mark_sent(wolts_dir: Path, out: Path) -> int:
    """Run only after the upload of out worked: its parts are not written out again."""
    archive = Path(wolts_dir) / ARCHIVE
    done = {f"{p.parent.name}/{p.name.removesuffix('.json.age')}"
            for p in (Path(out) / FORMAT).glob("*/*.json.age")}
    with os.fdopen(os.open(archive / ".lock", os.O_WRONLY | os.O_CREAT, 0o600), "w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        _write(archive / SENT, "".join(f"{part}\n" for part in sorted(_sent(archive) | done)).encode())
    return len(done)


LOCK_PROBE = "sessions/lock-probe"  # one fixed tiny object, so checks never pile up locked files
# only an object-lock refusal proves the lock; "locked" alone may be an account or a token
_LOCKED = re.compile(r"object[ _-]?lock|retention", re.IGNORECASE)


def check_lock(remote: str) -> tuple[str, str]:
    """Try to delete the probe with the lodge's own token: ("on", ...) only for
    the storage's object-lock refusal, ("off", ...) when it went, and ("unknown",
    ...) for anything else (no rclone, no probe, a network or token error)."""
    rclone, target = shutil.which("rclone"), f"{remote.rstrip('/')}/{LOCK_PROBE}"
    if not rclone:
        return "unknown", "rclone is not on PATH"
    try:
        seen = subprocess.run([rclone, "lsf", target], capture_output=True, text=True, timeout=120, check=False)
        if seen.returncode != 0 or not seen.stdout.strip():
            return "unknown", f"cannot see {target}: {seen.stderr.strip()[-300:] or 'missing'}"
        tried = subprocess.run([rclone, "deletefile", target], capture_output=True, text=True, timeout=120, check=False)
    except subprocess.TimeoutExpired:
        return "unknown", "rclone timed out"
    if tried.returncode == 0:
        return "off", f"the lodge's own token deleted {target}: the lock is OFF (put the probe back)"
    if _LOCKED.search(tried.stderr):
        return "on", "the storage refused the delete: the object is locked"
    return "unknown", tried.stderr.strip()[-300:] or f"rclone exit {tried.returncode}"
