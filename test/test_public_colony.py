import json
import subprocess
from pathlib import Path

import pytest

from woltspace.public_colony import (
    ColonyError,
    export_public_colony,
    inspect_public_colony,
    install_public_colony,
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


def test_export_is_small_allowlisted_and_deterministic(tmp_path):
    wolts = tmp_path / "wolts"
    wolt = make_wolt(wolts)
    make_skill(wolt)
    make_app(wolts)

    first = export_public_colony(
        wolts_dir=wolts,
        output=tmp_path / "one",
        name="starter-colony",
        wolt_names=["raccoon"],
        app_names=["tiny-app"],
        skills=["raccoon:public-craft"],
    )
    second = export_public_colony(
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
    export_public_colony(
        wolts_dir=source_wolts, output=package, name="starter-colony",
        wolt_names=["raccoon"], app_names=["tiny-app"],
    )
    subprocess.run(["git", "init", "-q", str(package)], check=True)
    install_root = make_template(tmp_path)
    target = tmp_path / "new-lodge"

    result = install_public_colony(
        source=package, wolts_dir=target, install_root=install_root,
    )

    assert result["wolts"] == ["raccoon"]
    config = json.loads((target / "raccoon/wolt/wolt.json").read_text())
    assert config["origin"] == "starter"
    assert config["provenance"]["format"] == "woltspace.public-colony/v1"
    assert "private current work" not in (target / "raccoon/wolt/memory/context.md").read_text()
    assert "Always be useful" in (target / "raccoon/CLAUDE.md").read_text()
    assert "# Platform rules" in (target / "raccoon/CLAUDE.md").read_text()
    installed_app = json.loads((target / "apps/tiny-app/woltspace.json").read_text())
    assert installed_app["port"] == 4000
    assert installed_app["public"] is False
    assert (target / "raccoon/.git").is_dir()

    with pytest.raises(ColonyError, match="overwrite"):
        install_public_colony(source=package, wolts_dir=target, install_root=install_root)


def test_export_rejects_secret_shaped_tracked_app_path(tmp_path):
    wolts = tmp_path / "wolts"
    make_wolt(wolts)
    app = make_app(wolts)
    (app / ".env.production").write_text("TOKEN=secret")
    subprocess.run(["git", "-C", str(app), "add", "-f", ".env.production"], check=True)

    with pytest.raises(ColonyError, match="secret-shaped"):
        export_public_colony(
            wolts_dir=wolts, output=tmp_path / "out", name="starter",
            wolt_names=["raccoon"], app_names=["tiny-app"],
        )


def test_inspect_rejects_credentials_and_absolute_home_paths(tmp_path):
    wolts = tmp_path / "wolts"
    make_wolt(wolts)
    package = tmp_path / "package"
    export_public_colony(
        wolts_dir=wolts, output=package, name="starter", wolt_names=["raccoon"],
    )
    (package / "wolts/raccoon/identity.md").write_text(
        "token ghs_abcdefghijklmnopqrstuvwxyz123456\n"
    )
    with pytest.raises(ColonyError, match="credential-like"):
        inspect_public_colony(package)

    (package / "wolts/raccoon/identity.md").write_text("See /Users/alice/private/file\n")
    with pytest.raises(ColonyError, match="home path"):
        inspect_public_colony(package)


def test_platform_skills_are_never_exported(tmp_path):
    wolts = tmp_path / "wolts"
    wolt = make_wolt(wolts)
    make_skill(wolt, "woltspace-notify")
    with pytest.raises(ColonyError, match="platform skill"):
        export_public_colony(
            wolts_dir=wolts, output=tmp_path / "out", name="starter",
            wolt_names=["raccoon"], skills=["raccoon:woltspace-notify"],
        )


def test_public_git_app_is_a_pinned_reference_not_a_copy(tmp_path):
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

    summary = export_public_colony(
        wolts_dir=wolts, output=tmp_path / "out", name="starter",
        wolt_names=["raccoon"], app_names=["tiny-app"],
    )

    reference = json.loads((summary.root / "apps/tiny-app/app.json").read_text())
    assert reference["url"] == "https://github.com/example/tiny-app.git"
    assert reference["revision"] == revision
    assert not (summary.root / "apps/tiny-app/server.mjs").exists()
    assert inspect_public_colony(summary.root).apps == ("tiny-app",)
