"""Git-friendly colony seed packages.

A colony seed is deliberately not a backup.  It contains authored identity,
rules, explicitly selected skills, and tracked app source.  Lived state is
never traversed, so sessions, memory, artifacts, caches, and credentials cannot
enter a package by accident.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable
from urllib.parse import urlsplit


FORMAT = "woltspace.colony-seed/v1"
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
    rb"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----)"
)
ABSOLUTE_HOME = re.compile(rb"(?:/Users/[^/\s]+/|/home/[^/\s]+/)")


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
            manifest_wolts.append({"name": wolt_name, "skills": exported_skills})

        manifest_apps = []
        for app_name in selected_apps:
            _valid_name(app_name, "app")
            app_source = wolts_dir / "apps" / app_name
            app_target = staging / "apps" / app_name
            app_export = _export_app(app_source, app_target, selected_wolts)
            manifest_apps.append({
                "name": app_name,
                "keeper": app_export["keeper"],
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
    except Exception:
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
        if path.is_symlink():
            raise SeedError(f"symlinks are not portable: {rel.as_posix()}")
    _valid_name(manifest.get("name", ""), "seed")
    wolts = tuple(_manifest_names(manifest, "wolts"))
    apps = tuple(_manifest_names(manifest, "apps"))
    if not wolts:
        raise SeedError("colony seed has no wolts")

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
        if entry.get("keeper") not in wolts:
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
        if SECRET_CONTENT.search(content):
            raise SeedError(f"credential-like content is not seed-safe: {rel}")
        if ABSOLUTE_HOME.search(content):
            raise SeedError(f"machine-specific home path is not portable: {rel}")
        digest.update(rel.encode() + b"\0")
        digest.update(content)
        if total > MAX_PACKAGE_BYTES:
            raise SeedError("colony seed exceeds 50 MiB review limit")
    return SeedSummary(root, manifest["name"], wolts, apps, file_count, total, digest.hexdigest())


def install_seed(
    *, source: str | Path, wolts_dir: Path, install_root: Path,
) -> dict:
    """Install independent starter copies from a local directory or Git URL."""
    cleanup: Path | None = None
    source_text = str(source)
    local = Path(source_text).expanduser()
    if local.is_dir():
        package = local.resolve()
        provenance = {"source": source_text, "format": FORMAT}
    else:
        cleanup = Path(tempfile.mkdtemp(prefix="woltspace-seed-source-"))
        package = cleanup / "repo"
        result = subprocess.run(
            ["git", "clone", "--depth", "1", "--", source_text, str(package)],
            capture_output=True, text=True,
        )
        if result.returncode:
            shutil.rmtree(cleanup, ignore_errors=True)
            raise SeedError(f"could not clone colony seed: {result.stderr.strip()}")
        provenance = {"source": source_text, "format": FORMAT}
        revision = subprocess.run(
            ["git", "-C", str(package), "rev-parse", "HEAD"],
            capture_output=True, text=True,
        )
        if revision.returncode == 0:
            provenance["revision"] = revision.stdout.strip()
    try:
        summary = inspect_seed(package)
        manifest = _read_json(package / "seed.json")
        wolts_dir = Path(wolts_dir).resolve()
        apps_dir = wolts_dir / "apps"
        conflicts = [name for name in summary.wolts if (wolts_dir / name).exists()]
        conflicts += [name for name in summary.apps if (apps_dir / name).exists()]
        if conflicts:
            raise SeedError(f"install would overwrite existing names: {', '.join(conflicts)}")
        wolts_dir.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".seed-install-", dir=wolts_dir))
        moved: list[Path] = []
        try:
            for entry in manifest["wolts"]:
                name = entry["name"]
                _stage_wolt(
                    package / "wolts" / name, stage / name, name,
                    Path(install_root) / "template", provenance,
                )
            ports = _used_ports(apps_dir)
            next_port = 4000
            for entry in manifest["apps"]:
                name = entry["name"]
                target = stage / "apps" / name
                distribution = entry.get("distribution", "bundled")
                if distribution == "bundled":
                    shutil.copytree(package / "apps" / name, target)
                    app_manifest = _read_json(target / "woltspace.json")
                else:
                    reference = _read_json(package / "apps" / name / "app.json")
                    _validate_git_reference(reference)
                    clone = subprocess.run(
                        ["git", "clone", "--quiet", "--no-checkout", "--", reference["url"], str(target)],
                        capture_output=True, text=True,
                    )
                    if clone.returncode:
                        raise SeedError(f"could not clone app {name}: {clone.stderr.strip()}")
                    checkout = subprocess.run(
                        ["git", "-C", str(target), "checkout", "--quiet", reference["revision"]],
                        capture_output=True, text=True,
                    )
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
                    app_manifest["source"] = provenance["source"]
                _write_json(target / "woltspace.json", app_manifest)
                ports.add(next_port)
                next_port += 1

            for name in summary.wolts:
                target = wolts_dir / name
                (stage / name).rename(target)
                moved.append(target)
            if summary.apps:
                apps_dir.mkdir(exist_ok=True)
            for name in summary.apps:
                target = apps_dir / name
                (stage / "apps" / name).rename(target)
                moved.append(target)
            return {
                "ok": True,
                "seed": summary.name,
                "wolts": list(summary.wolts),
                "apps": list(summary.apps),
                "source": provenance,
            }
        except Exception:
            for path in reversed(moved):
                shutil.rmtree(path, ignore_errors=True)
            raise
        finally:
            shutil.rmtree(stage, ignore_errors=True)
    finally:
        if cleanup:
            shutil.rmtree(cleanup, ignore_errors=True)


def _stage_wolt(source: Path, target: Path, name: str, template: Path, provenance: dict) -> None:
    if not template.is_dir():
        raise SeedError(f"Woltspace template not found: {template}")
    shutil.copytree(template, target)
    config = _read_json(source / "wolt.json")
    config["name"] = name
    config["origin"] = "starter"
    config["provenance"] = provenance
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
    manifest_path = source / "woltspace.json"
    if not manifest_path.is_file():
        raise SeedError(f"app manifest missing: {source.name}")
    manifest = _read_json(manifest_path)
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

    if "woltspace.json" not in files:
        raise SeedError(f"bundled app manifest must be tracked: {source.name}")
    for rel in files:
        _audit_relative_path(rel)
        src = source / rel
        if src.is_symlink() or not src.is_file():
            raise SeedError(f"app tracked path is not a regular file: {rel}")
        if rel == "woltspace.json":
            continue
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        content = subprocess.run(
            ["git", "-C", str(source), "show", f"HEAD:{rel}"], capture_output=True,
        )
        if content.returncode:
            raise SeedError(f"could not read tracked app source: {source.name}/{rel}")
        dst.write_bytes(content.stdout)
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
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
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
        path for path in root.rglob("*")
        if not path.is_dir() and ".git" not in path.relative_to(root).parts
    )


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
        "woltspace seed install .\n"
        "```\n\n"
        "Installing creates independent starter copies. Sessions, lived memory, app data, "
        "credentials, dependencies, and build artifacts are not included.\n"
    )
