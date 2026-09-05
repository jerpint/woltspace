"""Native path resolution before server/runtime imports."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from woltspace.layout import RuntimeLayout, installation_root


def test_source_installation_root_contains_runtime_and_assets():
    root = installation_root()
    assert (root / "server").is_dir()
    assert (root / "container" / "lib" / "sessions.py").is_file()
    assert (root / "public" / "static").is_dir()
    assert (root / "templates" / "home.html").is_file()


def test_native_layout_defaults_below_real_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    layout = RuntimeLayout.from_env({"HOME": str(tmp_path)})
    # expanduser reads the process HOME, matching a real CLI invocation.
    assert layout.wolts_dir == (tmp_path / ".woltspace" / "wolts").resolve()
    assert layout.state_root == layout.wolts_dir / ".space"
    assert layout.endpoint == "http://127.0.0.1:7777"
    assert layout.isolation == "host"


def test_explicit_layout_is_canonical_and_applies_environment(tmp_path, monkeypatch):
    root = installation_root()
    raw = tmp_path / "state" / ".." / "wolts"
    layout = RuntimeLayout.from_env({
        "WOLTS_DIR": str(raw),
        "WOLTSPACE_DIR": str(root),
        "WOLTSPACE_HOST": "localhost",
        "WOLTSPACE_PORT": "8123",
        "WOLTSPACE_ISOLATION": "external",
    })
    for key in ("WOLTS_DIR", "WOLT_DIR", "WOLTSPACE_DIR", "WOLTSPACE_ISOLATION", "PORT"):
        monkeypatch.delenv(key, raising=False)
    layout.apply_environment()

    assert layout.wolts_dir == (tmp_path / "wolts").resolve()
    assert os.environ["WOLTS_DIR"] == str(layout.wolts_dir)
    assert os.environ["WOLT_DIR"] == str(layout.wolts_dir)
    assert os.environ["WOLTSPACE_DIR"] == str(root)
    assert os.environ["WOLTSPACE_ISOLATION"] == "external"
    assert os.environ["PORT"] == "8123"
    assert sys.path[:2] == [str(layout.runtime_lib), str(root)]


def test_a_stale_install_pointer_loses_to_the_running_install(tmp_path):
    """A WOLTSPACE_DIR naming a directory with no container/lib is ignored."""
    ghost = tmp_path / "old-tool" / "_bundle"
    ghost.mkdir(parents=True)

    layout = RuntimeLayout.from_env({"WOLTSPACE_DIR": str(ghost)})

    assert layout.install_root == installation_root().resolve()
    assert layout.runtime_lib.is_dir()


def test_a_missing_install_pointer_loses_too(tmp_path):
    layout = RuntimeLayout.from_env({"WOLTSPACE_DIR": str(tmp_path / "never-existed")})

    assert layout.install_root == installation_root().resolve()


def test_a_live_install_pointer_is_still_honoured(tmp_path):
    other = tmp_path / "elsewhere" / "woltspace"
    (other / "container" / "lib").mkdir(parents=True)

    layout = RuntimeLayout.from_env({"WOLTSPACE_DIR": str(other)})

    assert layout.install_root == other.resolve()


def test_a_stale_pointer_is_not_stamped_back_into_the_environment(tmp_path, monkeypatch):
    ghost = tmp_path / "ghost"
    ghost.mkdir()
    layout = RuntimeLayout.from_env({"WOLTSPACE_DIR": str(ghost)})
    for key in ("WOLTS_DIR", "WOLT_DIR", "WOLTSPACE_DIR", "WOLTSPACE_ISOLATION", "PORT"):
        monkeypatch.delenv(key, raising=False)

    layout.apply_environment()

    assert os.environ["WOLTSPACE_DIR"] == str(installation_root().resolve())


def test_invalid_isolation_is_rejected():
    with pytest.raises(ValueError, match="isolation"):
        RuntimeLayout.from_env({"WOLTSPACE_ISOLATION": "wishful"})
