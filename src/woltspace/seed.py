"""Git-friendly colony seed packages.

A colony seed is deliberately not a backup.  It contains authored identity,
rules, explicitly selected skills, and tracked app source.  Lived state is
never traversed, so sessions, memory, artifacts, caches, and credentials cannot
enter a package by accident.
"""

from __future__ import annotations

import difflib
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable
from urllib.parse import urlsplit

from . import __version__


FORMAT = "woltspace.colony-seed/v1"
RECEIPT_FORMAT = "woltspace.seed-install/v1"
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_PACKAGE_BYTES = 50 * 1024 * 1024
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MANAGED_START = "<!-- WOLTSPACE:BEGIN"
MANAGED_END = "<!-- WOLTSPACE:END -->"
SEED_WOLT_FIELDS = ("name", "type", "role", "capabilities", "description")
SECRET_PARTS = {
    ".env", ".credentials.json", "credentials.json", "secrets.json",
    "id_rsa", "id_ed25519", "auth.json", "token.json",
}
SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx")
SECRET_CONTENT = re.compile(
    rb"(?:gh[ps]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|"
    rb"npm_[A-Za-z0-9]{20,}|pypi-[A-Za-z0-9_-]{20,}|"
    rb"xox(?:a|b|p|r|s)-[A-Za-z0-9-]{20,}|glpat-[A-Za-z0-9_-]{20,}|"
    rb"AKIA[0-9A-Z]{16}|"
    rb"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----)"
)
ABSOLUTE_HOME = re.compile(rb"(?:/Users/[^/\s]+/|/home/[^/\s]+/)")
COMPONENT_ID_RE = re.compile(r"^(wolt|app):[a-z0-9][a-z0-9_-]{0,63}$")
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
INSTALL_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")
LFS_POINTER = b"version https://git-lfs.github.com/spec/v1\n"
GIT_ENV = {
    "GIT_ALLOW_PROTOCOL": "https",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_LFS_SKIP_SMUDGE": "1",
}


class SeedError(ValueError):
    """A colony seed is unsafe, invalid, or cannot be installed."""


@dataclass(frozen=True)
class SeedSummary:
    root: Path
    name: str
    wolts: tuple[str, ...]
    apps: tuple[str, ...]
    files: int
    bytes: int
    digest: str

    def to_record(self) -> dict:
        return {
            "format": FORMAT,
            "name": self.name,
            "root": str(self.root),
            "wolts": list(self.wolts),
            "apps": list(self.apps),
            "files": self.files,
            "bytes": self.bytes,
            "sha256": self.digest,
        }


def create_seed(
    *, wolts_dir: Path, output: Path, name: str, wolt_names: Iterable[str],
    app_names: Iterable[str] = (), skills: Iterable[str] = (),
) -> SeedSummary:
    """Create one deterministic, allowlisted colony seed directory."""
    wolts_dir = Path(wolts_dir).resolve()
    output = Path(output).expanduser().resolve(strict=False)
    _valid_name(name, "seed")
    selected_wolts = _unique(wolt_names)
    selected_apps = _unique(app_names)
    if not selected_wolts:
        raise SeedError("select at least one wolt")
    if output.exists():
        raise SeedError(f"output already exists: {output}")
    skill_map = _parse_skills(skills, selected_wolts)

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.partial-", dir=output.parent))
    try:
        manifest_wolts = []
        for wolt_name in selected_wolts:
            _valid_name(wolt_name, "wolt")
            source = wolts_dir / wolt_name
            config_path = source / "wolt" / "wolt.json"
            identity_path = source / "wolt" / "memory" / "identity.md"
            if not config_path.is_file() or not identity_path.is_file():
                raise SeedError(f"wolt is missing identity/config: {wolt_name}")
            config = _read_json(config_path)
            seed_config = {
                key: config[key] for key in SEED_WOLT_FIELDS if key in config
            }
            seed_config["name"] = wolt_name
            target = staging / "wolts" / wolt_name
            target.mkdir(parents=True)
            _write_json(target / "wolt.json", seed_config)
            _copy_seed_text(identity_path, target / "identity.md")
            rules = _authored_rules(source / "CLAUDE.md")
            (target / "rules.md").write_text(rules, encoding="utf-8")

            exported_skills = []
            for skill_name in skill_map.get(wolt_name, ()):
                _valid_name(skill_name, "skill")
                if skill_name.startswith("woltspace-"):
                    raise SeedError(f"platform skill cannot be exported: {skill_name}")
                skill_source = source / ".claude" / "skills" / skill_name
                skill_target = target / "skills" / skill_name
                _copy_explicit_tree(skill_source, skill_target)
                exported_skills.append(skill_name)
            manifest_wolts.append({
                "id": f"wolt:{wolt_name}",
                "name": wolt_name,
                "skills": exported_skills,
            })

        manifest_apps = []
        for app_name in selected_apps:
            _valid_name(app_name, "app")
            app_source = wolts_dir / "apps" / app_name
            app_target = staging / "apps" / app_name
            app_export = _export_app(app_source, app_target, selected_wolts)
            manifest_apps.append({
                "id": f"app:{app_name}",
                "name": app_name,
                "keeper": app_export["keeper"],
                "keeper_id": f"wolt:{app_export['keeper']}",
                "distribution": app_export["distribution"],
            })

        manifest = {
            "format": FORMAT,
            "name": name,
            "wolts": manifest_wolts,
            "apps": manifest_apps,
        }
        _write_json(staging / "seed.json", manifest)
        (staging / "README.md").write_text(_readme(manifest), encoding="utf-8")
        _write_gitignore(staging / ".gitignore")
        inspect_seed(staging)
        staging.rename(output)
        return inspect_seed(output)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def inspect_seed(root: Path) -> SeedSummary:
    """Validate a colony seed without modifying it."""
    root = Path(root).resolve()
    manifest_path = root / "seed.json"
    if manifest_path.is_symlink():
        raise SeedError("seed.json must not be a symlink")
    if not manifest_path.is_file():
        raise SeedError(f"not a colony seed (missing seed.json): {root}")
    manifest = _read_json(manifest_path)
    if manifest.get("format") != FORMAT:
        raise SeedError(f"unsupported seed format: {manifest.get('format')!r}")
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if rel.parts and rel.parts[0] == ".git":
            continue
        if ".git" in rel.parts:
            raise SeedError(f"nested Git internals are not seed-safe: {rel.as_posix()}")
        if path.is_symlink():
            raise SeedError(f"symlinks are not portable: {rel.as_posix()}")
    _valid_name(manifest.get("name", ""), "seed")
    wolts = tuple(_manifest_names(manifest, "wolts"))
    apps = tuple(_manifest_names(manifest, "apps"))
    if not wolts:
        raise SeedError("colony seed has no wolts")
    component_ids: list[str] = []
    for kind, entries in (("wolt", manifest["wolts"]), ("app", manifest["apps"])):
        for entry in entries:
            component_id = entry.get("id", f"{kind}:{entry['name']}")
            if not isinstance(component_id, str) or not COMPONENT_ID_RE.fullmatch(component_id):
                raise SeedError(f"invalid seed component id: {component_id!r}")
            if not component_id.startswith(f"{kind}:"):
                raise SeedError(f"seed component id has wrong kind: {component_id}")
            component_ids.append(component_id)
    if len(component_ids) != len(set(component_ids)):
        raise SeedError("seed component ids must be unique")

    expected_roots = {"seed.json", "README.md", ".gitignore", "wolts", "apps"}
    for child in root.iterdir():
        if child.name == ".git":
            continue
        if child.name not in expected_roots:
            raise SeedError(f"unexpected top-level path: {child.name}")

    for entry in manifest["wolts"]:
        name = entry["name"]
        base = root / "wolts" / name
        for required in ("wolt.json", "identity.md", "rules.md"):
            if not (base / required).is_file():
                raise SeedError(f"wolt {name} is missing {required}")
        config = _read_json(base / "wolt.json")
        unexpected = set(config) - set(SEED_WOLT_FIELDS)
        if unexpected:
            raise SeedError(f"wolt {name} has non-seed config: {sorted(unexpected)}")
        if config.get("name") != name:
            raise SeedError(f"wolt directory/config name mismatch: {name}")
        declared_skills = set(entry.get("skills", []))
        skills_dir = base / "skills"
        actual_skills = {p.name for p in skills_dir.iterdir()} if skills_dir.is_dir() else set()
        if declared_skills != actual_skills:
            raise SeedError(f"wolt {name} skill manifest does not match files")

    for entry in manifest["apps"]:
        name = entry["name"]
        distribution = entry.get("distribution", "bundled")
        if distribution == "bundled":
            app_manifest = _read_json(root / "apps" / name / "woltspace.json")
        elif distribution == "git":
            reference = _read_json(root / "apps" / name / "app.json")
            _validate_git_reference(reference)
            app_manifest = reference.get("manifest")
            if not isinstance(app_manifest, dict):
                raise SeedError(f"app {name} Git reference has no manifest")
        else:
            raise SeedError(f"app {name} has unknown distribution: {distribution}")
        if app_manifest.get("name") != name or app_manifest.get("keeper") != entry.get("keeper"):
            raise SeedError(f"app manifest does not match seed.json: {name}")
        if "port" in app_manifest or app_manifest.get("public") is not False:
            raise SeedError(f"app {name} contains live deployment state")
        keeper = entry.get("keeper")
        keeper_id = entry.get("keeper_id", f"wolt:{keeper}")
        if keeper not in wolts or keeper_id not in component_ids:
            raise SeedError(f"app {name} keeper is not included")

    file_count = 0
    total = 0
    digest = hashlib.sha256()
    for path in _package_files(root):
        rel = path.relative_to(root).as_posix()
        _audit_relative_path(rel)
        if path.is_symlink():
            raise SeedError(f"symlinks are not portable: {rel}")
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise SeedError(f"file exceeds 5 MiB seed limit: {rel}")
        total += size
        file_count += 1
        content = path.read_bytes()
        mode = _portable_mode(path)
        if content.startswith(LFS_POINTER):
            raise SeedError(f"Git LFS pointers are not seed packages: {rel}")
        if SECRET_CONTENT.search(content):
            raise SeedError(f"credential-like content is not seed-safe: {rel}")
        if ABSOLUTE_HOME.search(content):
            raise SeedError(f"machine-specific home path is not portable: {rel}")
        digest.update(rel.encode() + b"\0" + mode.encode() + b"\0")
        digest.update(content)
        if total > MAX_PACKAGE_BYTES:
            raise SeedError("colony seed exceeds 50 MiB review limit")
    return SeedSummary(root, manifest["name"], wolts, apps, file_count, total, digest.hexdigest())


@contextmanager
def resolved_seed(source: str | Path, *, ref: str | None = None):
    """Yield a validated local package plus immutable source metadata."""
    source_text = str(source)
    local = Path(source_text).expanduser()
    cleanup: Path | None = None
    try:
        if ref is not None:
            _validate_ref(ref)
        if local.is_dir() and ref is None:
            package = local.resolve()
            warnings: list[str] = []
            status = subprocess.run(
                ["git", "-C", str(package), "status", "--porcelain", "--untracked-files=all"],
                capture_output=True, text=True,
            )
            if status.returncode == 0 and status.stdout.strip():
                warnings.append(
                    "local seed has uncommitted or untracked package files; "
                    "its digest may not match the pushed revision"
                )
            source_record = {
                "url": None,
                "requested_ref": None,
                "resolved_commit": None,
                "tracking_ref": None,
                "warnings": warnings,
            }
        elif local.is_dir():
            cleanup = Path(tempfile.mkdtemp(prefix="woltspace-seed-source-"))
            commit = _resolve_local_commit(local.resolve(), ref)
            package = cleanup / "package"
            _materialize_git_tree(local.resolve(), commit, package)
            source_record = {
                "url": None,
                "requested_ref": ref,
                "resolved_commit": commit,
                "tracking_ref": None,
                "warnings": [],
            }
        else:
            if ref is None:
                raise SeedError("remote seed sources require --ref with a tag or full commit")
            _validate_seed_url(source_text)
            cleanup = Path(tempfile.mkdtemp(prefix="woltspace-seed-source-"))
            repository = cleanup / "repo"
            _git(["init", "-q", str(repository)])
            _git(["-C", str(repository), "remote", "add", "origin", source_text])
            fetch_ref = _remote_fetch_ref(source_text, ref)
            fetched = _git(
                ["-C", str(repository), "fetch", "--quiet", "--depth", "1", "origin", fetch_ref],
                check=False,
            )
            if fetched.returncode:
                raise SeedError(f"could not fetch colony seed ref: {fetched.stderr.strip()}")
            commit = _git(
                ["-C", str(repository), "rev-parse", "FETCH_HEAD^{commit}"],
            ).stdout.strip()
            if not FULL_SHA_RE.fullmatch(commit):
                raise SeedError("Git did not resolve the seed ref to a full commit")
            package = cleanup / "package"
            _materialize_git_tree(repository, commit, package)
            source_record = {
                "url": source_text,
                "requested_ref": ref,
                "resolved_commit": commit,
                "tracking_ref": None,
                "warnings": [],
            }
        summary = inspect_seed(package)
        source_record["seed_digest"] = summary.digest
        yield package, summary, source_record
    finally:
        if cleanup:
            shutil.rmtree(cleanup, ignore_errors=True)


def inspect_seed_source(source: str | Path, *, ref: str | None = None) -> dict:
    """Inspect a local or pinned remote seed and return its full trust preview."""
    with resolved_seed(source, ref=ref) as (package, summary, source_record):
        manifest = _normalized_manifest(_read_json(package / "seed.json"))
        return {
            **summary.to_record(),
            "root": str(package) if source_record["url"] is None and ref is None else None,
            "source": source_record,
            "warnings": source_record.get("warnings", []),
            "components": _component_preview(package, manifest),
            "trust": (
                "Wolt rules and skills become instructions followed with the wolt's local "
                "access. Apps and skill scripts are code that may run on this machine."
            ),
        }


def _validate_seed_url(value: str) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https" or not parsed.hostname or parsed.username
        or parsed.password or parsed.query or parsed.fragment
    ):
        raise SeedError("seed sources must use a credential-free HTTPS Git URL")


def _validate_ref(value: str) -> None:
    if (
        not isinstance(value, str) or not REF_RE.fullmatch(value)
        or ".." in value or "@{" in value or value.endswith((".", "/", ".lock"))
        or "//" in value
    ):
        raise SeedError(f"invalid seed Git ref: {value!r}")


def _git(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update(GIT_ENV)
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, env=env, check=False,
    )
    if check and result.returncode:
        raise SeedError((result.stderr or "Git operation failed").strip())
    return result


def _resolve_local_commit(repository: Path, ref: str) -> str:
    if not FULL_SHA_RE.fullmatch(ref):
        tag_ref = f"refs/tags/{ref}"
        verified = subprocess.run(
            ["git", "-C", str(repository), "show-ref", "--verify", "--", tag_ref],
            capture_output=True, text=True,
        )
        if verified.returncode:
            raise SeedError("moving refs are not import identities; use an exact tag or full commit")
        ref = tag_ref
    result = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", f"{ref}^{{commit}}"],
        capture_output=True, text=True,
    )
    if result.returncode or not FULL_SHA_RE.fullmatch(result.stdout.strip()):
        raise SeedError(f"could not resolve local seed ref: {ref}")
    return result.stdout.strip()


def _remote_fetch_ref(url: str, ref: str) -> str:
    if FULL_SHA_RE.fullmatch(ref):
        return ref
    tag_ref = f"refs/tags/{ref}"
    tags = _git(["ls-remote", "--tags", "--refs", "--", url, tag_ref], check=False)
    if tags.returncode:
        raise SeedError(f"could not resolve seed tag: {tags.stderr.strip()}")
    found = {
        line.partition("\t")[2]
        for line in tags.stdout.splitlines()
        if line.partition("\t")[1]
    }
    if found != {tag_ref}:
        raise SeedError("moving refs are not import identities; use an exact tag or full commit")
    return tag_ref


def _materialize_git_tree(repository: Path, commit: str, target: Path) -> None:
    target.mkdir(parents=True)
    listing = subprocess.run(
        ["git", "-C", str(repository), "ls-tree", "-r", "-z", commit],
        capture_output=True, check=False,
    )
    if listing.returncode:
        raise SeedError("could not read seed Git tree")
    for raw in filter(None, listing.stdout.split(b"\0")):
        header, separator, raw_path = raw.partition(b"\t")
        if not separator:
            raise SeedError("malformed Git tree entry")
        try:
            mode, object_type, oid = header.decode("ascii").split()
            rel = raw_path.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as exc:
            raise SeedError("seed Git tree paths must be UTF-8") from exc
        _audit_relative_path(rel)
        if object_type != "blob" or mode not in {"100644", "100755"}:
            raise SeedError(f"unsupported Git seed entry mode/type: {rel}")
        blob = subprocess.run(
            ["git", "-C", str(repository), "cat-file", "blob", oid],
            capture_output=True, check=False,
        )
        if blob.returncode:
            raise SeedError(f"could not read seed Git blob: {rel}")
        if blob.stdout.startswith(LFS_POINTER):
            raise SeedError(f"Git LFS pointers are not seed packages: {rel}")
        destination = target / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(blob.stdout)
        destination.chmod(0o755 if mode == "100755" else 0o644)


def _normalized_manifest(manifest: dict) -> dict:
    value = json.loads(json.dumps(manifest))
    for kind, key in (("wolt", "wolts"), ("app", "apps")):
        for entry in value.get(key, []):
            entry.setdefault("id", f"{kind}:{entry.get('name', '')}")
            if kind == "app":
                entry.setdefault("keeper_id", f"wolt:{entry.get('keeper', '')}")
    return value


def _component_preview(package: Path, manifest: dict) -> list[dict]:
    preview: list[dict] = []
    for entry in manifest["wolts"]:
        root = package / "wolts" / entry["name"]
        skill_files = []
        skills = root / "skills"
        if skills.is_dir():
            for path in _package_files(skills):
                rel = path.relative_to(root).as_posix()
                skill_files.append({
                    "path": rel,
                    "executable": _portable_mode(path) == "100755",
                })
        preview.append({
            "id": entry["id"],
            "kind": "wolt",
            "name": entry["name"],
            "rules": (root / "rules.md").read_text(encoding="utf-8"),
            "identity": (root / "identity.md").read_text(encoding="utf-8"),
            "skill_files": skill_files,
        })
    for entry in manifest["apps"]:
        root = package / "apps" / entry["name"]
        preview.append({
            "id": entry["id"],
            "kind": "app",
            "name": entry["name"],
            "keeper_id": entry["keeper_id"],
            "distribution": entry.get("distribution", "bundled"),
            "files": [path.relative_to(root).as_posix() for path in _package_files(root)],
        })
    return preview


def install_seed(
    *, source: str | Path, wolts_dir: Path, install_root: Path,
    ref: str | None = None, wolt_names: Iterable[str] = (),
    app_names: Iterable[str] = (),
) -> dict:
    """Import selected seed components as fresh starters into a live lodge."""
    wolts_dir = Path(wolts_dir).resolve()
    wolts_dir.mkdir(parents=True, exist_ok=True)
    with resolved_seed(source, ref=ref) as (package, summary, source_record):
        manifest = _normalized_manifest(_read_json(package / "seed.json"))
        selected_wolts, selected_apps, required = _select_components(
            manifest, wolt_names=wolt_names, app_names=app_names,
        )
        apps_dir = wolts_dir / "apps"
        state_root = wolts_dir / ".space" / "seeds"
        with _seed_import_lock(state_root):
            _validate_apps_destination(apps_dir, bool(selected_apps))
            conflicts = [
                entry["name"] for entry in selected_wolts
                if _path_occupied(wolts_dir / entry["name"])
            ]
            conflicts += [
                entry["name"] for entry in selected_apps
                if _path_occupied(apps_dir / entry["name"])
            ]
            if conflicts:
                raise SeedError(f"import would overwrite existing names: {', '.join(conflicts)}")

            install_id = str(uuid.uuid4())
            stage = Path(tempfile.mkdtemp(prefix=".seed-import-", dir=wolts_dir))
            state_stage = state_root / f".staging-{install_id}"
            install_state = state_root / "installs" / install_id
            moved: list[Path] = []
            try:
                state_stage.mkdir(parents=True, mode=0o700)
                base = state_stage / "base"
                components = _stage_base_projection(
                    package, manifest, selected_wolts, selected_apps, base,
                )
                base_digest = _digest_tree(base)
                seed_awareness = {
                    "install_id": install_id,
                    "source_url": source_record["url"],
                    "requested_ref": source_record["requested_ref"],
                    "resolved_commit": source_record["resolved_commit"],
                    "digest": summary.digest,
                    "tracking_ref": source_record["tracking_ref"],
                }
                for entry in selected_wolts:
                    awareness = {**seed_awareness, "component_id": entry["id"]}
                    _stage_wolt(
                        package / "wolts" / entry["name"],
                        stage / entry["name"], entry["name"],
                        Path(install_root) / "template", awareness,
                    )
                ports = _used_ports(apps_dir)
                next_port = 4000
                for entry in selected_apps:
                    name = entry["name"]
                    target = stage / "apps" / name
                    distribution = entry.get("distribution", "bundled")
                    if distribution == "bundled":
                        shutil.copytree(package / "apps" / name, target)
                        app_manifest = _read_json(target / "woltspace.json")
                    else:
                        reference = _read_json(package / "apps" / name / "app.json")
                        _validate_git_reference(reference)
                        clone = _git([
                            "clone", "--quiet", "--no-checkout", "--",
                            reference["url"], str(target),
                        ], check=False)
                        if clone.returncode:
                            raise SeedError(f"could not clone app {name}: {clone.stderr.strip()}")
                        checkout = _git([
                            "-C", str(target), "checkout", "--quiet", reference["revision"],
                        ], check=False)
                        if checkout.returncode:
                            raise SeedError(f"could not check out app {name}: {checkout.stderr.strip()}")
                        _audit_checkout(target)
                        app_manifest = reference["manifest"]
                    while next_port in ports:
                        next_port += 1
                    app_manifest["port"] = next_port
                    app_manifest["public"] = False
                    if distribution == "git":
                        app_manifest["source"] = f"{reference['url']}@{reference['revision']}"
                    else:
                        app_manifest["source"] = source_record["url"] or str(source)
                    _write_json(target / "woltspace.json", app_manifest)
                    ports.add(next_port)
                    next_port += 1

                receipt = _receipt(
                    install_id=install_id, summary=summary, source=source_record,
                    base_digest=base_digest, components=components,
                )
                _write_private_json(state_stage / "receipt.json", receipt)

                for entry in selected_wolts:
                    name = entry["name"]
                    target = wolts_dir / name
                    if _path_occupied(target):
                        raise SeedError(f"import destination appeared during commit: {target}")
                    (stage / name).rename(target)
                    moved.append(target)
                if selected_apps:
                    _validate_apps_destination(apps_dir, True)
                    apps_dir.mkdir(exist_ok=True)
                for entry in selected_apps:
                    name = entry["name"]
                    target = apps_dir / name
                    if _path_occupied(target):
                        raise SeedError(f"import destination appeared during commit: {target}")
                    (stage / "apps" / name).rename(target)
                    moved.append(target)
                install_state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if _path_occupied(install_state):
                    raise SeedError(f"seed install state already exists: {install_id}")
                state_stage.rename(install_state)
                return {
                    "ok": True,
                    "seed": summary.name,
                    "install_id": install_id,
                    "wolts": [entry["name"] for entry in selected_wolts],
                    "apps": [entry["name"] for entry in selected_apps],
                    "required": required,
                    "source": source_record,
                    "receipt": str(install_state / "receipt.json"),
                }
            except BaseException as exc:
                blocked: list[str] = []
                for path in reversed(moved):
                    sessions = path / ".state" / "sessions"
                    if path.parent == wolts_dir and sessions.is_dir() and any(sessions.glob("*.json")):
                        blocked.append(path.name)
                        continue
                    shutil.rmtree(path, ignore_errors=True)
                shutil.rmtree(state_stage, ignore_errors=True)
                if blocked:
                    raise SeedError(
                        "import rollback preserved wolts with session records; "
                        f"manual recovery required: {', '.join(blocked)}"
                    ) from exc
                raise
            finally:
                shutil.rmtree(stage, ignore_errors=True)


def _select_components(
    manifest: dict, *, wolt_names: Iterable[str], app_names: Iterable[str],
) -> tuple[list[dict], list[dict], list[str]]:
    requested_wolts = set(wolt_names)
    requested_apps = set(app_names)
    all_wolts = {entry["name"]: entry for entry in manifest["wolts"]}
    all_apps = {entry["name"]: entry for entry in manifest["apps"]}
    if not requested_wolts and not requested_apps:
        return list(manifest["wolts"]), list(manifest["apps"]), []
    unknown = sorted((requested_wolts - all_wolts.keys()) | (requested_apps - all_apps.keys()))
    if unknown:
        raise SeedError(f"unknown seed components: {', '.join(unknown)}")
    required: list[str] = []
    for name in sorted(requested_apps):
        keeper = all_apps[name]["keeper"]
        if keeper not in requested_wolts:
            requested_wolts.add(keeper)
            required.append(keeper)
    return (
        [entry for entry in manifest["wolts"] if entry["name"] in requested_wolts],
        [entry for entry in manifest["apps"] if entry["name"] in requested_apps],
        required,
    )


@contextmanager
def _seed_import_lock(state_root: Path):
    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state_root, 0o700)
    lock = state_root / "import.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _stage_base_projection(
    package: Path, manifest: dict, wolts: list[dict], apps: list[dict], target: Path,
) -> list[dict]:
    target.mkdir(parents=True, mode=0o700)
    selected_manifest = {
        "format": manifest["format"],
        "name": manifest["name"],
        "wolts": wolts,
        "apps": apps,
    }
    _write_private_json(target / "seed.json", selected_manifest)
    components: list[dict] = []
    for entry in wolts:
        name = entry["name"]
        source = package / "wolts" / name
        destination = target / "wolts" / name
        destination.mkdir(parents=True, mode=0o700)
        portable_config = _read_json(source / "wolt.json")
        portable_config = {
            key: portable_config[key] for key in SEED_WOLT_FIELDS if key in portable_config
        }
        portable_config["name"] = name
        _write_private_json(destination / "wolt.json", portable_config)
        for filename in ("identity.md", "rules.md"):
            _copy_private_file(source / filename, destination / filename)
        skills = source / "skills"
        if skills.is_dir():
            _copy_private_tree(skills, destination / "skills")
        owned_paths = [
            {
                "seed_path": "identity.md",
                "local_path": "wolt/memory/identity.md",
                "mode": "manual-review",
            },
            {
                "seed_path": "rules.md",
                "local_path": "CLAUDE.md",
                "mode": "region",
            },
            {
                "seed_path": "wolt.json",
                "local_path": "wolt/wolt.json",
                "mode": "fields",
                "fields": list(SEED_WOLT_FIELDS),
            },
        ]
        for skill in entry.get("skills", []):
            owned_paths.append({
                "seed_path": f"skills/{skill}/**",
                "local_path": f".claude/skills/{skill}/**",
                "mode": "file",
            })
        components.append({
            "id": entry["id"], "kind": "wolt", "source_name": name,
            "local_name": name, "owned_paths": sorted(owned_paths, key=lambda item: item["seed_path"]),
        })
    for entry in apps:
        name = entry["name"]
        source = package / "apps" / name
        destination = target / "apps" / name
        _copy_private_tree(source, destination)
        owned = []
        for path in _package_files(source):
            rel = path.relative_to(source).as_posix()
            owned.append({"seed_path": rel, "local_path": rel, "mode": "file"})
        components.append({
            "id": entry["id"], "kind": "app", "source_name": name,
            "local_name": name, "owned_paths": owned,
        })
    return sorted(components, key=lambda item: item["id"])


def _copy_private_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.write_bytes(source.read_bytes())
    target.chmod(0o700 if _portable_mode(source) == "100755" else 0o600)


def _copy_private_tree(source: Path, target: Path) -> None:
    for path in _package_files(source):
        _copy_private_file(path, target / path.relative_to(source))


def _digest_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in _package_files(root):
        rel = path.relative_to(root).as_posix()
        digest.update(rel.encode() + b"\0" + _portable_mode(path).encode() + b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _receipt(
    *, install_id: str, summary: SeedSummary, source: dict,
    base_digest: str, components: list[dict],
) -> dict:
    invoked_by = {"kind": "cli"}
    if os.environ.get("WOLTSPACE_WOLT_NAME"):
        invoked_by = {
            "kind": "wolt",
            "wolt": os.environ["WOLTSPACE_WOLT_NAME"],
            "session": os.environ.get("WOLTSPACE_WOLT_SESSION") or None,
        }
    return {
        "format": RECEIPT_FORMAT,
        "install_id": install_id,
        "installed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "installer_version": __version__,
        "seed": {
            "format": FORMAT,
            "name": summary.name,
            "digest": summary.digest,
            "base_digest": base_digest,
        },
        "source": {
            "url": source["url"],
            "requested_ref": source["requested_ref"],
            "resolved_commit": source["resolved_commit"],
            "tracking_ref": source["tracking_ref"],
        },
        "components": components,
        "rename_map": {},
        "forked_from": None,
        "invoked_by": invoked_by,
    }


def _write_private_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = json.dumps(value, indent=2, sort_keys=True) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(data)


def seed_status(
    *, wolts_dir: Path, wolt: str | None = None,
    install_id: str | None = None, to_ref: str | None = None,
) -> dict:
    """Read installed lineage and optionally diff one exact upstream ref."""
    wolts_dir = Path(wolts_dir).resolve()
    warnings: list[str] = []
    if wolt:
        _valid_name(wolt, "wolt")
        config = _read_json(wolts_dir / wolt / "wolt" / "wolt.json")
        mirror = config.get("seed")
        if not isinstance(mirror, dict) or not isinstance(mirror.get("install_id"), str):
            raise SeedError(f"wolt has no seed import lineage: {wolt}")
        install_id = mirror["install_id"]
    if not install_id:
        raise SeedError("seed status requires --wolt or --install-id")
    if not INSTALL_ID_RE.fullmatch(install_id):
        raise SeedError("invalid seed install id")
    receipt_path = wolts_dir / ".space" / "seeds" / "installs" / install_id / "receipt.json"
    receipt = _read_json(receipt_path)
    if receipt.get("format") != RECEIPT_FORMAT or receipt.get("install_id") != install_id:
        raise SeedError("seed import receipt is invalid")
    base = receipt_path.parent / "base"
    if _digest_tree(base) != receipt.get("seed", {}).get("base_digest"):
        raise SeedError("seed import base snapshot digest mismatch")
    for component in receipt["components"]:
        if component["kind"] != "wolt":
            continue
        path = wolts_dir / component["local_name"] / "wolt" / "wolt.json"
        try:
            current = _read_json(path).get("seed")
        except SeedError:
            current = None
        expected = {
            "install_id": install_id,
            "component_id": component["id"],
            "source_url": receipt["source"]["url"],
            "requested_ref": receipt["source"]["requested_ref"],
            "resolved_commit": receipt["source"]["resolved_commit"],
            "digest": receipt["seed"]["digest"],
            "tracking_ref": receipt["source"]["tracking_ref"],
        }
        if current != expected:
            warnings.append(f"wolt manifest lineage differs from receipt: {component['local_name']}")
    result = {
        "ok": True,
        "install_id": install_id,
        "seed": receipt["seed"],
        "source": receipt["source"],
        "components": receipt["components"],
        "warnings": warnings,
        "candidate_tags": [],
        "candidate": None,
        "changes": [],
    }
    url = receipt["source"].get("url")
    if not url:
        if to_ref:
            raise SeedError("local seed imports have no remote source")
        return result
    if to_ref is None:
        result["candidate_tags"] = _newer_semver_tags(
            url, receipt["source"].get("requested_ref")
        )
        return result
    with resolved_seed(url, ref=to_ref) as (package, summary, source_record):
        manifest = _normalized_manifest(_read_json(package / "seed.json"))
        result["candidate"] = {**source_record, "seed_digest": summary.digest}
        result["changes"] = _diff_receipt_base(
            receipt=receipt, base=base, package=package, manifest=manifest,
        )
    return result


def _newer_semver_tags(url: str, current: str | None) -> list[str]:
    _validate_seed_url(url)
    result = _git(["ls-remote", "--tags", "--refs", "--", url], check=False)
    if result.returncode:
        raise SeedError(f"could not list seed tags: {result.stderr.strip()}")
    current_version = _semver(current) if current else None
    tags = []
    for line in result.stdout.splitlines():
        _, separator, refname = line.partition("\t")
        if not separator or not refname.startswith("refs/tags/"):
            continue
        tag = refname.removeprefix("refs/tags/")
        version = _semver(tag)
        if version is not None and (current_version is None or version > current_version):
            tags.append((version, tag))
    return [tag for _, tag in sorted(tags)]


def _semver(value: str | None) -> tuple[int, int, int] | None:
    if not value:
        return None
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", value)
    return tuple(map(int, match.groups())) if match else None


def _diff_receipt_base(*, receipt: dict, base: Path, package: Path, manifest: dict) -> list[dict]:
    candidate_by_id = {
        entry["id"]: ("wolt", entry) for entry in manifest["wolts"]
    } | {
        entry["id"]: ("app", entry) for entry in manifest["apps"]
    }
    changes: list[dict] = []
    for component in receipt["components"]:
        component_id = component["id"]
        candidate = candidate_by_id.get(component_id)
        if candidate is None:
            changes.append({"component": component_id, "kind": "removed"})
            continue
        kind, entry = candidate
        old_root = base / ("wolts" if kind == "wolt" else "apps") / component["source_name"]
        new_root = package / ("wolts" if kind == "wolt" else "apps") / entry["name"]
        old_files = {
            path.relative_to(old_root).as_posix(): path for path in _package_files(old_root)
        }
        new_files = {
            path.relative_to(new_root).as_posix(): path for path in _package_files(new_root)
        }
        for rel in sorted(set(old_files) | set(new_files)):
            old = old_files.get(rel)
            new = new_files.get(rel)
            old_bytes = old.read_bytes() if old else b""
            new_bytes = new.read_bytes() if new else b""
            old_mode = _portable_mode(old) if old else None
            new_mode = _portable_mode(new) if new else None
            if old_bytes == new_bytes and old_mode == new_mode:
                continue
            label = "content"
            if kind == "app" or (rel.startswith("skills/") and new_mode == "100755"):
                label = "executable"
            elif kind == "wolt" and (rel == "rules.md" or rel.startswith("skills/")):
                label = "instructions"
            elif kind == "wolt" and rel == "identity.md":
                label = "manual-review"
            elif kind == "wolt" and rel == "wolt.json":
                label = "configuration"
            diff = ""
            try:
                diff = "".join(difflib.unified_diff(
                    old_bytes.decode("utf-8").splitlines(keepends=True),
                    new_bytes.decode("utf-8").splitlines(keepends=True),
                    fromfile=f"installed/{component_id}/{rel}",
                    tofile=f"candidate/{component_id}/{rel}",
                ))
            except UnicodeDecodeError:
                diff = "binary content changed"
            changes.append({
                "component": component_id,
                "kind": "changed",
                "path": rel,
                "classification": label,
                "old_mode": old_mode,
                "new_mode": new_mode,
                "diff": diff,
            })
    installed_ids = {component["id"] for component in receipt["components"]}
    for component_id in sorted(candidate_by_id.keys() - installed_ids):
        changes.append({"component": component_id, "kind": "available"})
    return changes


def _stage_wolt(source: Path, target: Path, name: str, template: Path, seed: dict) -> None:
    if not template.is_dir():
        raise SeedError(f"Woltspace template not found: {template}")
    shutil.copytree(template, target)
    config = _read_json(source / "wolt.json")
    for key in ("seed", "provenance", "origin"):
        config.pop(key, None)
    config["name"] = name
    config["origin"] = "starter"
    config["seed"] = seed
    memory = target / "wolt" / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / "identity.md", memory / "identity.md")
    (memory / "context.md").write_text("# Context\n\nNew independent starter copy.\n", encoding="utf-8")
    (memory / "learnings.md").write_text("# Learnings\n\n", encoding="utf-8")
    _write_json(target / "wolt" / "wolt.json", config)
    template_rules = (target / "CLAUDE.md").read_text(encoding="utf-8")
    managed = _managed_rules(template_rules)
    authored = (source / "rules.md").read_text(encoding="utf-8").strip()
    (target / "CLAUDE.md").write_text(
        managed.rstrip() + "\n\n" + authored + "\n", encoding="utf-8"
    )
    agents = target / "AGENTS.md"
    try:
        agents.symlink_to("CLAUDE.md")
    except OSError:
        shutil.copy2(target / "CLAUDE.md", agents)
    for skill_name in _read_skill_dirs(source):
        _copy_explicit_tree(
            source / "skills" / skill_name,
            target / ".claude" / "skills" / skill_name,
        )
    subprocess.run(["git", "init", "-q", str(target)], check=False)


def _export_app(source: Path, target: Path, selected_wolts: list[str]) -> dict:
    if not source.is_dir():
        raise SeedError(f"app not found: {source.name}")
    top = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
    )
    if top.returncode or Path(top.stdout.strip()).resolve() != source.resolve():
        raise SeedError(f"app must be its own Git repository: {source.name}")
    listed = subprocess.run(
        ["git", "-C", str(source), "ls-files", "-z"],
        capture_output=True,
    )
    if listed.returncode:
        raise SeedError(f"could not list tracked app source: {source.name}")
    files = sorted(filter(None, listed.stdout.decode().split("\0")))
    if "woltspace.json" not in files:
        raise SeedError(f"app manifest must be tracked: {source.name}")
    dirty = subprocess.run(
        ["git", "-C", str(source), "status", "--porcelain", "--untracked-files=no"],
        capture_output=True, text=True,
    )
    if dirty.returncode:
        raise SeedError(f"could not inspect app worktree: {source.name}")
    if dirty.stdout.strip():
        raise SeedError(
            f"app has uncommitted tracked changes; commit or discard them first: {source.name}"
        )
    manifest = _read_git_json(source, "woltspace.json")
    if manifest.get("name") != source.name:
        raise SeedError(f"app directory/manifest name mismatch: {source.name}")
    keeper = manifest.get("keeper")
    if keeper not in selected_wolts:
        raise SeedError(f"app {source.name} keeper {keeper!r} is not selected")
    manifest.pop("port", None)
    manifest["public"] = False
    manifest["source"] = None

    remote = subprocess.run(
        ["git", "-C", str(source), "remote", "get-url", "origin"],
        capture_output=True, text=True,
    )
    revision = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    )
    if remote.returncode == 0 and revision.returncode == 0:
        url = remote.stdout.strip()
        reference = {
            "distribution": "git",
            "url": url,
            "revision": revision.stdout.strip(),
            "manifest": manifest,
        }
        _validate_git_reference(reference)
        _write_json(target / "app.json", reference)
        return {"keeper": keeper, "distribution": "git"}

    for rel in files:
        _audit_relative_path(rel)
        src = source / rel
        if src.is_symlink() or not src.is_file():
            raise SeedError(f"app tracked path is not a regular file: {rel}")
        if rel == "woltspace.json":
            continue
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(_read_git_file(source, rel))
        mode_result = subprocess.run(
            ["git", "-C", str(source), "ls-tree", "HEAD", "--", rel],
            capture_output=True, text=True,
        )
        if mode_result.returncode or not mode_result.stdout:
            raise SeedError(f"could not read tracked app mode: {source.name}/{rel}")
        git_mode = mode_result.stdout.split(None, 1)[0]
        if git_mode not in {"100644", "100755"}:
            raise SeedError(f"app tracked path has unsupported mode: {rel}")
        dst.chmod(0o755 if git_mode == "100755" else 0o644)
    _write_json(target / "woltspace.json", manifest)
    return {"keeper": keeper, "distribution": "bundled"}


def _validate_git_reference(reference: dict) -> None:
    if reference.get("distribution") != "git":
        raise SeedError("invalid Git app reference")
    url = reference.get("url")
    revision = reference.get("revision")
    if not isinstance(url, str):
        raise SeedError("Git app reference has no URL")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https" or not parsed.hostname or parsed.username
        or parsed.password or parsed.query or parsed.fragment
    ):
        raise SeedError("Git app references must use a credential-free HTTPS URL")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise SeedError("Git app reference must pin a full commit SHA")


def _audit_checkout(root: Path) -> None:
    """Reject unsafe filesystem shapes in a re-derived app checkout."""
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if rel.parts and rel.parts[0] == ".git":
            continue
        _audit_relative_path(rel.as_posix())
        if path.is_symlink():
            raise SeedError(f"Git app contains a non-portable symlink: {rel.as_posix()}")


def _read_git_file(source: Path, rel: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(source), "show", f"HEAD:{rel}"], capture_output=True,
    )
    if result.returncode:
        raise SeedError(f"could not read tracked app source: {source.name}/{rel}")
    return result.stdout


def _read_git_json(source: Path, rel: str) -> dict:
    try:
        value = json.loads(_read_git_file(source, rel).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SeedError(f"invalid JSON in tracked app source: {source.name}/{rel}") from exc
    if not isinstance(value, dict):
        raise SeedError(f"JSON object required in tracked app source: {source.name}/{rel}")
    return value


def _parse_skills(values: Iterable[str], wolts: list[str]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for value in values:
        if ":" not in value:
            raise SeedError("skills must be selected as WOLT:SKILL")
        wolt, skill = value.split(":", 1)
        if wolt not in wolts:
            raise SeedError(f"skill selects an unselected wolt: {wolt}")
        result.setdefault(wolt, []).append(skill)
    return {key: _unique(values) for key, values in result.items()}


def _authored_rules(path: Path) -> str:
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8")
    if MANAGED_START not in text:
        return text.strip() + "\n"
    start = text.index(MANAGED_START)
    end = text.find(MANAGED_END, start)
    if end < 0:
        raise SeedError(f"managed rules block is malformed: {path}")
    return (text[:start] + text[end + len(MANAGED_END):]).strip() + "\n"


def _managed_rules(text: str) -> str:
    start = text.find(MANAGED_START)
    end = text.find(MANAGED_END, start)
    if start < 0 or end < 0:
        raise SeedError("installed Woltspace template has no managed rules block")
    return text[start:end + len(MANAGED_END)]


def _copy_explicit_tree(source: Path, target: Path) -> None:
    if not source.is_dir() or source.is_symlink():
        raise SeedError(f"selected seed directory is missing or a symlink: {source}")
    for path in sorted(source.rglob("*")):
        if path.is_dir():
            continue
        rel = path.relative_to(source).as_posix()
        _audit_relative_path(rel)
        if path.is_symlink() or not path.is_file():
            raise SeedError(f"selected seed path is not a regular file: {rel}")
        destination = target / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)


def _copy_seed_text(source: Path, target: Path) -> None:
    try:
        text = source.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise SeedError(f"seed identity must be UTF-8 text: {source}") from exc
    target.write_text(text, encoding="utf-8")


def _audit_relative_path(value: str) -> None:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise SeedError(f"unsafe package path: {value}")
    lower_parts = [part.lower() for part in path.parts]
    for part in lower_parts:
        if part == ".git" or part == "node_modules" or part == "__pycache__":
            raise SeedError(f"generated/private path is not seed-safe: {value}")
        if part in SECRET_PARTS or part.startswith(".env.") and not part.endswith((".example", ".sample")):
            raise SeedError(f"secret-shaped path is not seed-safe: {value}")
        if part.endswith(SECRET_SUFFIXES):
            raise SeedError(f"key-shaped path is not seed-safe: {value}")


def _manifest_names(manifest: dict, key: str) -> list[str]:
    entries = manifest.get(key)
    if not isinstance(entries, list):
        raise SeedError(f"seed.json {key} must be a list")
    names = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise SeedError(f"seed.json {key} entries must be objects")
        name = entry.get("name", "")
        _valid_name(name, key[:-1])
        names.append(name)
    if len(names) != len(set(names)):
        raise SeedError(f"seed.json has duplicate {key}")
    return names


def _package_files(root: Path) -> list[Path]:
    return sorted(
        [
            path for path in root.rglob("*")
            if not path.is_dir() and path.relative_to(root).parts[:1] != (".git",)
        ],
        key=lambda path: path.relative_to(root).as_posix().encode(),
    )


def _portable_mode(path: Path) -> str:
    mode = path.stat().st_mode
    if not stat.S_ISREG(mode):
        raise SeedError(f"seed path is not a regular file: {path}")
    return "100755" if mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH) else "100644"


def _read_skill_dirs(source: Path) -> list[str]:
    directory = source / "skills"
    return sorted(path.name for path in directory.iterdir() if path.is_dir()) if directory.is_dir() else []


def _used_ports(apps_dir: Path) -> set[int]:
    used = set()
    if apps_dir.is_dir():
        for manifest in apps_dir.glob("*/woltspace.json"):
            try:
                port = _read_json(manifest).get("port")
                if isinstance(port, int):
                    used.add(port)
            except SeedError:
                continue
    return used


def _path_occupied(path: Path) -> bool:
    """Treat broken symlinks as occupied too."""
    return path.exists() or path.is_symlink()


def _validate_apps_destination(apps_dir: Path, needed: bool) -> None:
    if not _path_occupied(apps_dir):
        return
    if apps_dir.is_symlink() or not apps_dir.is_dir():
        if needed:
            raise SeedError(f"apps destination must be a real directory: {apps_dir}")
        return


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SeedError(f"invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise SeedError(f"JSON object required: {path}")
    return value


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _valid_name(value: str, kind: str) -> None:
    if not isinstance(value, str) or not NAME_RE.fullmatch(value):
        raise SeedError(f"invalid {kind} name: {value!r}")


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _write_gitignore(path: Path) -> None:
    path.write_text(
        ".DS_Store\n.env\n.env.*\n!.env.example\n!.env.sample\n"
        "node_modules/\n.venv/\n__pycache__/\n*.pyc\ndist/\nbuild/\n",
        encoding="utf-8",
    )


def _readme(manifest: dict) -> str:
    wolts = ", ".join(entry["name"] for entry in manifest["wolts"])
    apps = ", ".join(entry["name"] for entry in manifest["apps"]) or "none"
    return (
        f"# {manifest['name']}\n\n"
        "A Woltspace colony seed: portable starter identity and source, not a backup.\n\n"
        f"- Wolts: {wolts}\n- Apps: {apps}\n\n"
        "```sh\n"
        "woltspace seed inspect .\n"
        "woltspace seed import .\n"
        "```\n\n"
        "Installing creates independent starter copies. Sessions, lived memory, app data, "
        "credentials, dependencies, and build artifacts are not included.\n"
    )
