import json
import subprocess
from pathlib import Path

import pytest

from woltspace.seed import (
    MAX_FILE_BYTES,
    SeedError,
    _install_wolt_skill_site,
    _split_seed_git_source,
    create_seed,
    inspect_seed,
    install_seed,
)


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def make_template(root: Path) -> Path:
    template = root / "install" / "template"
    (template / "wolt" / "site").mkdir(parents=True)
    (template / "wolt" / "site" / "index.html").write_text("starter")
    (template / "CLAUDE.md").write_text(
        "<!-- WOLTSPACE:BEGIN — auto-managed, do not edit -->\n"
        "# Platform rules\n"
        "<!-- WOLTSPACE:END -->\n\n"
        "# Placeholder\n"
    )
    return template.parent


def make_wolt(root: Path, name: str = "raccoon") -> Path:
    wolt = root / name
    write_json(wolt / "wolt" / "wolt.json", {
        "name": name,
        "type": "raccoon",
        "role": "Helpful builder",
        "capabilities": ["build"],
        "description": "A public starter",
        "harness": "codex",
        "model": "private-machine-choice",
        "origin": "user",
    })
    memory = wolt / "wolt" / "memory"
    memory.mkdir(parents=True)
    (memory / "identity.md").write_text(f"# {name}\n\nA careful raccoon.\n")
    (memory / "context.md").write_text("private current work\n")
    (memory / "learnings.md").write_text("private lived lesson\n")
    (memory / "archive").mkdir()
    (memory / "archive" / "conversations.md").write_text("secret history\n")
    (wolt / "wolt" / "sparks").mkdir()
    (wolt / "wolt" / "sparks" / "artifact.bin").write_bytes(b"artifact")
    (wolt / ".claude" / "sessions").mkdir(parents=True)
    (wolt / ".claude" / "sessions" / "history.jsonl").write_text("private")
    (wolt / "CLAUDE.md").write_text(
        "<!-- WOLTSPACE:BEGIN — auto-managed, do not edit -->\n"
        "private machine platform instructions\n"
        "<!-- WOLTSPACE:END -->\n\n"
        f"# {name}\n\nAlways be useful.\n"
    )
    return wolt


def make_skill(wolt: Path, name: str = "public-craft") -> None:
    skill = wolt / ".claude" / "skills" / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Public Craft\n\nDo the craft.\n")


def make_site_skill(wolt: Path, name: str = "raccoon") -> None:
    skill = wolt / ".claude" / "skills" / f"{name}-site"
    (skill / "site").mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Raccoon Site\n\nOwn the starter site.\n")
    (skill / "site" / "index.html").write_text("<h1>skill site</h1>\n")
    (skill / "site" / "site.css").write_text("h1 { color: green; }\n")


def make_app(root: Path, name: str = "tiny-app", keeper: str = "raccoon") -> Path:
    app = root / "apps" / name
    app.mkdir(parents=True)
    write_json(app / "woltspace.json", {
        "name": name,
        "description": "Tiny",
        "stack": "node",
        "start": "node server.mjs",
        "port": 4888,
        "keeper": keeper,
        "public": True,
        "source": "local-private-source",
    })
    (app / "server.mjs").write_text("console.log('hello')\n")
    (app / "ignored.log").write_text("runtime data\n")
    subprocess.run(["git", "init", "-q", str(app)], check=True)
    subprocess.run(
        ["git", "-C", str(app), "add", "woltspace.json", "server.mjs"], check=True
    )
    subprocess.run([
        "git", "-C", str(app), "-c", "user.name=Test",
        "-c", "user.email=test@example.invalid", "commit", "-qm", "initial",
    ], check=True)
    return app


def test_seed_is_small_allowlisted_and_deterministic(tmp_path):
    wolts = tmp_path / "wolts"
    wolt = make_wolt(wolts)
    make_skill(wolt)
    make_app(wolts)

    first = create_seed(
        wolts_dir=wolts,
        output=tmp_path / "one",
        name="starter-colony",
        wolt_names=["raccoon"],
        app_names=["tiny-app"],
        skills=["raccoon:public-craft"],
    )
    second = create_seed(
        wolts_dir=wolts,
        output=tmp_path / "two",
        name="starter-colony",
        wolt_names=["raccoon"],
        app_names=["tiny-app"],
        skills=["raccoon:public-craft"],
    )

    assert first.digest == second.digest
    assert first.bytes < 10_000
    files = {p.relative_to(first.root).as_posix() for p in first.root.rglob("*") if p.is_file()}
    assert "wolts/raccoon/identity.md" in files
    assert "wolts/raccoon/rules.md" in files
    assert "wolts/raccoon/skills/public-craft/SKILL.md" in files
    assert "apps/tiny-app/server.mjs" in files
    assert not any("context" in path or "sessions" in path or "sparks" in path for path in files)
    assert "apps/tiny-app/ignored.log" not in files
    config = json.loads((first.root / "wolts/raccoon/wolt.json").read_text())
    assert "harness" not in config and "model" not in config and "origin" not in config
    rules = (first.root / "wolts/raccoon/rules.md").read_text()
    assert "Always be useful" in rules
    assert "private machine" not in rules
    app = json.loads((first.root / "apps/tiny-app/woltspace.json").read_text())
    assert "port" not in app
    assert app["public"] is False and app["source"] is None
    assert not any((first.root / name).exists() for name in (
        "LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING",
    ))


@pytest.mark.parametrize("name", ["LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING"])
def test_seed_accepts_root_license_as_validated_package_metadata(tmp_path, name):
    source_wolts = tmp_path / "source-wolts"
    make_wolt(source_wolts)
    package = tmp_path / "package"
    create_seed(
        wolts_dir=source_wolts, output=package, name="starter",
        wolt_names=["raccoon"],
    )
    (package / name).write_text("MIT License\n", encoding="utf-8")

    summary = inspect_seed(package)
    install_root = make_template(tmp_path)
    target = tmp_path / "new-lodge"
    install_seed(source=package, wolts_dir=target, install_root=install_root)

    assert summary.files > 0
    assert not any(path.name in {"LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING"}
                   for path in (target / "raccoon").rglob("*"))


def test_seed_rejects_non_text_or_oversized_root_license(tmp_path):
    wolts = tmp_path / "wolts"
    make_wolt(wolts)
    package = tmp_path / "package"
    create_seed(
        wolts_dir=wolts, output=package, name="starter", wolt_names=["raccoon"],
    )
    license_path = package / "LICENSE"
    license_path.write_bytes(b"\xff\xfe")
    with pytest.raises(SeedError, match="UTF-8 text"):
        inspect_seed(package)

    license_path.write_bytes(b"x" * (MAX_FILE_BYTES + 1))
    with pytest.raises(SeedError, match="exceeds 5 MiB"):
        inspect_seed(package)


def test_install_creates_fresh_independent_starters(tmp_path):
    source_wolts = tmp_path / "source-wolts"
    make_wolt(source_wolts)
    make_app(source_wolts)
    package = tmp_path / "package"
    create_seed(
        wolts_dir=source_wolts, output=package, name="starter-colony",
        wolt_names=["raccoon"], app_names=["tiny-app"],
    )
    subprocess.run(["git", "init", "-q", str(package)], check=True)
    install_root = make_template(tmp_path)
    target = tmp_path / "new-lodge"

    result = install_seed(
        source=package, wolts_dir=target, install_root=install_root,
    )

    assert result["seed"] == "starter-colony"
    assert result["wolts"] == ["raccoon"]
    config = json.loads((target / "raccoon/wolt/wolt.json").read_text())
    assert config["origin"] == "starter"
    assert config["provenance"]["format"] == "woltspace.colony-seed/v1"
    assert "private current work" not in (target / "raccoon/wolt/memory/context.md").read_text()
    assert "Always be useful" in (target / "raccoon/CLAUDE.md").read_text()
    assert "# Platform rules" in (target / "raccoon/CLAUDE.md").read_text()
    installed_app = json.loads((target / "apps/tiny-app/woltspace.json").read_text())
    assert installed_app["port"] == 4000
    assert installed_app["public"] is False
    assert (target / "raccoon/.git").is_dir()

    with pytest.raises(SeedError, match="overwrite"):
        install_seed(source=package, wolts_dir=target, install_root=install_root)


def test_install_uses_named_site_skill_for_initial_site(tmp_path):
    source_wolts = tmp_path / "source-wolts"
    source = make_wolt(source_wolts)
    make_site_skill(source)
    package = tmp_path / "package"
    create_seed(
        wolts_dir=source_wolts, output=package, name="starter",
        wolt_names=["raccoon"], skills=["raccoon:raccoon-site"],
    )
    install_root = make_template(tmp_path)

    target = tmp_path / "new-lodge"
    install_seed(source=package, wolts_dir=target, install_root=install_root)

    installed = target / "raccoon" / "wolt" / "site"
    assert (installed / "index.html").read_text() == "<h1>skill site</h1>\n"
    assert (installed / "site.css").is_file()
    assert not (installed / "style.css").exists()


@pytest.mark.parametrize("site_state", ["missing", "starter"])
def test_site_skill_installs_into_missing_or_starter_site(tmp_path, site_state):
    wolt = tmp_path / "raccoon"
    write_json(wolt / "wolt" / "wolt.json", {"name": "raccoon", "type": "raccoon"})
    make_site_skill(wolt)
    if site_state == "starter":
        from wolts import scaffold_starter_site
        site = wolt / "wolt" / "site"
        site.mkdir(parents=True)
        scaffold_starter_site(site, "raccoon", "raccoon")

    assert _install_wolt_skill_site(wolt, "raccoon") is True
    assert (wolt / "wolt" / "site" / "index.html").read_text() == "<h1>skill site</h1>\n"


def test_site_skill_does_not_replace_edited_site(tmp_path):
    from wolts import scaffold_starter_site

    wolt = tmp_path / "raccoon"
    write_json(wolt / "wolt" / "wolt.json", {"name": "raccoon", "type": "raccoon"})
    make_site_skill(wolt)
    site = wolt / "wolt" / "site"
    site.mkdir(parents=True)
    scaffold_starter_site(site, "raccoon", "raccoon")
    (site / "index.html").write_text("owner edited this site\n")

    assert _install_wolt_skill_site(wolt, "raccoon") is False
    assert (site / "index.html").read_text() == "owner edited this site\n"


def _bare_seed_repository(tmp_path: Path) -> tuple[Path, str, str]:
    source_wolts = tmp_path / "source-wolts"
    make_wolt(source_wolts)
    package = tmp_path / "package"
    create_seed(
        wolts_dir=source_wolts, output=package, name="starter-colony",
        wolt_names=["raccoon"],
    )
    subprocess.run(["git", "init", "-q", str(package)], check=True)
    subprocess.run(["git", "-C", str(package), "add", "."], check=True)
    subprocess.run([
        "git", "-C", str(package), "-c", "user.name=Test",
        "-c", "user.email=test@example.invalid", "commit", "-qm", "first",
    ], check=True)
    first = subprocess.run(
        ["git", "-C", str(package), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    (package / "wolts/raccoon/identity.md").write_text("# New tip identity\n")
    subprocess.run(["git", "-C", str(package), "add", "."], check=True)
    subprocess.run([
        "git", "-C", str(package), "-c", "user.name=Test",
        "-c", "user.email=test@example.invalid", "commit", "-qm", "second",
    ], check=True)
    tip = subprocess.run(
        ["git", "-C", str(package), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    bare = tmp_path / "seed.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(package), str(bare)], check=True)
    return bare, first, tip


def test_install_git_source_at_full_commit_uses_exact_revision(tmp_path):
    bare, first, _tip = _bare_seed_repository(tmp_path)
    url = bare.as_uri()
    install_root = make_template(tmp_path)
    target = tmp_path / "new-lodge"

    result = install_seed(
        source=f"{url}@{first}", wolts_dir=target, install_root=install_root,
    )

    assert result["source"]["url"] == url
    assert result["source"]["revision"] == first
    assert result["source"]["source"] == f"{url}@{first}"
    identity = (target / "raccoon/wolt/memory/identity.md").read_text()
    assert "A careful raccoon" in identity
    provenance = json.loads((target / "raccoon/wolt/wolt.json").read_text())["provenance"]
    assert provenance["url"] == url
    assert provenance["revision"] == first


def test_install_unpinned_git_source_keeps_tip_behavior(tmp_path):
    bare, _first, tip = _bare_seed_repository(tmp_path)
    url = bare.as_uri()

    result = install_seed(
        source=url, wolts_dir=tmp_path / "new-lodge",
        install_root=make_template(tmp_path),
    )

    assert result["source"]["url"] == url
    assert result["source"]["revision"] == tip


def test_install_git_source_reports_missing_pinned_commit(tmp_path):
    bare, _first, _tip = _bare_seed_repository(tmp_path)
    missing = "0" * 40

    with pytest.raises(SeedError, match=f"could not check out colony seed revision {missing}"):
        install_seed(
            source=f"{bare.as_uri()}@{missing}", wolts_dir=tmp_path / "new-lodge",
            install_root=make_template(tmp_path),
        )


def test_pinned_source_split_preserves_git_at_host_urls():
    source = "git@example.com:owner/seed.git"
    assert _split_seed_git_source(source) == (source, None)
    revision = "a" * 40
    assert _split_seed_git_source(f"{source}@{revision}") == (source, revision)


def test_seed_rejects_secret_shaped_tracked_app_path(tmp_path):
    wolts = tmp_path / "wolts"
    make_wolt(wolts)
    app = make_app(wolts)
    (app / ".env.production").write_text("TOKEN=secret")
    subprocess.run(["git", "-C", str(app), "add", "-f", ".env.production"], check=True)
    subprocess.run([
        "git", "-C", str(app), "-c", "user.name=Test",
        "-c", "user.email=test@example.invalid", "commit", "-qm", "add fixture",
    ], check=True)

    with pytest.raises(SeedError, match="secret-shaped"):
        create_seed(
            wolts_dir=wolts, output=tmp_path / "out", name="starter",
            wolt_names=["raccoon"], app_names=["tiny-app"],
        )


def test_seed_rejects_dirty_tracked_app_manifest(tmp_path):
    wolts = tmp_path / "wolts"
    make_wolt(wolts)
    app = make_app(wolts)
    manifest = json.loads((app / "woltspace.json").read_text())
    manifest["start"] = "python unexpected.py"
    write_json(app / "woltspace.json", manifest)

    with pytest.raises(SeedError, match="uncommitted tracked changes"):
        create_seed(
            wolts_dir=wolts, output=tmp_path / "out", name="starter",
            wolt_names=["raccoon"], app_names=["tiny-app"],
        )


def test_inspect_rejects_credentials_and_absolute_home_paths(tmp_path):
    wolts = tmp_path / "wolts"
    make_wolt(wolts)
    package = tmp_path / "package"
    create_seed(
        wolts_dir=wolts, output=package, name="starter", wolt_names=["raccoon"],
    )
    (package / "wolts/raccoon/identity.md").write_text(
        "token ghs_abcdefghijklmnopqrstuvwxyz123456\n"
    )
    with pytest.raises(SeedError, match="credential-like"):
        inspect_seed(package)

    (package / "wolts/raccoon/identity.md").write_text("See /Users/alice/private/file\n")
    with pytest.raises(SeedError, match="home path"):
        inspect_seed(package)


def test_platform_skills_are_never_seeded(tmp_path):
    wolts = tmp_path / "wolts"
    wolt = make_wolt(wolts)
    make_skill(wolt, "woltspace-notify")
    with pytest.raises(SeedError, match="platform skill"):
        create_seed(
            wolts_dir=wolts, output=tmp_path / "out", name="starter",
            wolt_names=["raccoon"], skills=["raccoon:woltspace-notify"],
        )


def test_https_git_app_is_a_pinned_reference_not_a_copy(tmp_path):
    wolts = tmp_path / "wolts"
    make_wolt(wolts)
    app = make_app(wolts)
    subprocess.run([
        "git", "-C", str(app), "remote", "add", "origin",
        "https://github.com/example/tiny-app.git",
    ], check=True)
    revision = subprocess.run(
        ["git", "-C", str(app), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()

    summary = create_seed(
        wolts_dir=wolts, output=tmp_path / "out", name="starter",
        wolt_names=["raccoon"], app_names=["tiny-app"],
    )

    reference = json.loads((summary.root / "apps/tiny-app/app.json").read_text())
    assert reference["url"] == "https://github.com/example/tiny-app.git"
    assert reference["revision"] == revision
    assert not (summary.root / "apps/tiny-app/server.mjs").exists()
    assert inspect_seed(summary.root).apps == ("tiny-app",)


@pytest.mark.parametrize("suffix", ["?token=not-safe", "#not-safe"])
def test_seed_rejects_git_url_query_or_fragment(tmp_path, suffix):
    wolts = tmp_path / "wolts"
    make_wolt(wolts)
    app = make_app(wolts)
    subprocess.run([
        "git", "-C", str(app), "remote", "add", "origin",
        f"https://example.com/tiny-app.git{suffix}",
    ], check=True)

    with pytest.raises(SeedError, match="credential-free HTTPS"):
        create_seed(
            wolts_dir=wolts, output=tmp_path / "out", name="starter",
            wolt_names=["raccoon"], app_names=["tiny-app"],
        )


@pytest.mark.parametrize("credential", [
    "npm_abcdefghijklmnopqrstuvwxyz",
    "pypi-abcdefghijklmnopqrstuvwxyz",
    "xoxb-123456789012-abcdefghijklmnop",
    "glpat-abcdefghijklmnopqrstuvwx",
    "AKIA1234567890ABCDEF",
])
def test_inspect_rejects_common_credential_families(tmp_path, credential):
    wolts = tmp_path / "wolts"
    make_wolt(wolts)
    package = tmp_path / "package"
    create_seed(
        wolts_dir=wolts, output=package, name="starter", wolt_names=["raccoon"],
    )
    (package / "wolts/raccoon/identity.md").write_text(f"credential {credential}\n")

    with pytest.raises(SeedError, match="credential-like"):
        inspect_seed(package)


def test_install_rejects_apps_symlink_without_touching_target(tmp_path):
    source_wolts = tmp_path / "source-wolts"
    make_wolt(source_wolts)
    make_app(source_wolts)
    package = tmp_path / "package"
    create_seed(
        wolts_dir=source_wolts, output=package, name="starter",
        wolt_names=["raccoon"], app_names=["tiny-app"],
    )
    install_root = make_template(tmp_path)
    target = tmp_path / "new-lodge"
    target.mkdir()
    external = tmp_path / "external-apps"
    external.mkdir()
    (target / "apps").symlink_to(external, target_is_directory=True)

    with pytest.raises(SeedError, match="real directory"):
        install_seed(source=package, wolts_dir=target, install_root=install_root)

    assert list(external.iterdir()) == []
    assert not (target / "raccoon").exists()


def test_install_rolls_back_on_keyboard_interrupt(tmp_path, monkeypatch):
    source_wolts = tmp_path / "source-wolts"
    make_wolt(source_wolts, "first")
    make_wolt(source_wolts, "second")
    package = tmp_path / "package"
    create_seed(
        wolts_dir=source_wolts, output=package, name="starter",
        wolt_names=["first", "second"],
    )
    install_root = make_template(tmp_path)
    target = tmp_path / "new-lodge"
    original_rename = Path.rename

    def interrupt_second(source, destination):
        if source.name == "second" and source.parent.name.startswith(".seed-install-"):
            raise KeyboardInterrupt
        return original_rename(source, destination)

    monkeypatch.setattr(Path, "rename", interrupt_second)
    with pytest.raises(KeyboardInterrupt):
        install_seed(source=package, wolts_dir=target, install_root=install_root)

    assert not (target / "first").exists()
    assert not (target / "second").exists()


def test_install_syncs_platform_skills_before_returning(tmp_path):
    source_wolts = tmp_path / "source-wolts"
    make_wolt(source_wolts)
    package = tmp_path / "package"
    create_seed(
        wolts_dir=source_wolts, output=package, name="starter",
        wolt_names=["raccoon"],
    )
    install_root = make_template(tmp_path)
    skill = install_root / "container" / "skills" / "start-chat"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: start-chat\ndescription: boot\n---\nhello\n"
    )
    target = tmp_path / "new-lodge"

    install_seed(source=package, wolts_dir=target, install_root=install_root)

    delivered = target / "raccoon" / ".claude" / "skills" / "woltspace-start-chat"
    assert delivered.is_dir()
    assert "name: woltspace-start-chat" in (delivered / "SKILL.md").read_text()
    assert (target / "raccoon" / ".agents" / "skills").exists()


def test_seed_and_backup_are_distinct_top_level_commands():
    from woltspace.cli import build_parser

    parser = build_parser()
    top_level = next(
        action for action in parser._actions if getattr(action, "choices", None)
    ).choices
    assert "seed" in top_level
    assert "backup" in top_level
    assert "restore" in top_level
    assert "colony" not in top_level

    seed_parser = top_level["seed"]
    seed_verbs = next(
        action for action in seed_parser._actions if getattr(action, "choices", None)
    ).choices
    assert set(seed_verbs) == {"create", "inspect", "install"}
