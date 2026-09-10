"""The update skill must describe the runtime we actually ship.

The skill body is prose, so nothing but a test stops it drifting back to the
container era — a wolt reading it will happily follow whatever it says. These
are the assertions that would have caught the old body the moment the platform
stopped being a checkout: no clone to pull, both published artifacts named,
the control-plane restart spelled out, and the migration directory sitting
where the wheel can carry it.

Plain file reads only. Nothing here touches a control plane, a registry, or
the network.
"""

import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SKILL = REPO / "container" / "skills" / "update" / "SKILL.md"
MIGRATIONS = REPO / "container" / "migrations"


@pytest.fixture(scope="module")
def body() -> str:
    assert SKILL.is_file(), f"the update skill is missing: {SKILL}"
    return SKILL.read_text()


# ---------------------------------------------------------------------------
# What the skill must no longer say
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("banned, why", [
    ("/workspace/woltspace", "the platform is an installed package, not a clone at a fixed path"),
    ("git pull", "there is no checkout to pull — updating is a reinstall"),
])
def test_no_container_era_mechanics(body, banned, why):
    assert banned not in body, f"update skill still says {banned!r}: {why}"


# ---------------------------------------------------------------------------
# What it must say
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("needle, why", [
    ("uv tool install", "the python half is installed with uv"),
    ("@woltspace/tui", "the tui half is its own install and must be named"),
    ("woltspace stop", "the update stops the control plane"),
    ("woltspace start", "and starts it again — that is where skills resync"),
])
def test_names_the_real_mechanics(body, needle, why):
    assert needle in body, f"update skill never mentions {needle!r}: {why}"


def test_frontmatter_is_intact(body):
    """The name and the invocable flag are how the skill is delivered at all."""
    assert body.startswith("---\n")
    front = body.split("---\n", 2)[1]
    assert "name: update" in front
    assert "user_invocable: true" in front


# ---------------------------------------------------------------------------
# Migrations ride inside the wheel
# ---------------------------------------------------------------------------

def test_migrations_live_under_container(body):
    assert MIGRATIONS.is_dir(), f"migrations must live at {MIGRATIONS} so the wheel carries them"
    assert any(MIGRATIONS.glob("*.md")), "no migration documents found"
    assert "container/migrations/" in body, \
        "the skill must look for migrations where the wheel actually puts them"


def test_wheel_force_include_covers_container():
    """`container` is force-included wholesale, so `container/migrations` rides along.

    Asserting the mapping rather than a built artifact: this is the line that
    decides whether a migration reaches the machine that needs it, and it is
    one edit away from being narrowed to something that misses the directory.
    """
    manifest = tomllib.loads((REPO / "pyproject.toml").read_text())
    include = manifest["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert include.get("container") == "woltspace/_bundle/container", (
        "container/ must be force-included at woltspace/_bundle/container — "
        f"got {include.get('container')!r}"
    )
    excluded = manifest["tool"]["hatch"]["build"].get("exclude", [])
    assert not any("migration" in pattern for pattern in excluded), \
        f"a build exclude would drop the migrations: {excluded}"


def test_skill_reads_migrations_from_the_install_root(body):
    """After the install, from the path the CLI reports — not from a checkout."""
    assert "woltspace paths" in body
    assert "install_root" in body
