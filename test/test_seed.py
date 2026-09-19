import json
import subprocess
from pathlib import Path

import pytest

from woltspace.seed import (
    SeedError,
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
