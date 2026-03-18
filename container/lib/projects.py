"""
Woltspace project schema — defines the woltspace.json manifest.

Usage:
    from projects import WoltspaceProject, load_project, discover_projects

    project = load_project("/workspace/wolts/neowolt/wolt/projects/forj")
    all_projects = discover_projects()
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from pydantic import BaseModel, Field

WOLTS_DIR = Path("/workspace/wolts")

VALID_STACKS = {"python", "vite", "node", "html"}

MANIFEST = "woltspace.json"

# Wolt type emojis — reserved, never used as random defaults
WOLT_EMOJIS = {"🦫", "🦝", "🦦", "🐺", "🐶", "🕷️", "🐻", "🐼"}

# Forest creatures — used as random defaults for new projects
FOREST_EMOJIS = ["🦅", "🦉", "🐿️", "🦊", "🐝", "🦌", "🐾", "🐸", "🦋", "🐛", "🪲", "🐞"]


def random_emoji() -> str:
    """Pick a random forest creature emoji for a new project."""
    return random.choice(FOREST_EMOJIS)


class WoltspaceProject(BaseModel):
    """v0.1 woltspace.json schema.

    Every project lives in wolts/<wolt>/wolt/projects/<name>/woltspace.json.
    Wolt populates the manifest, platform enforces completeness.
    Null fields = explicit todos. Can't start a project with start=None.
    """

    woltspace_version: str = Field(default="0.1", description="Schema version")
    name: str = Field(description="Project name (matches directory name)")
    description: str | None = Field(default=None, description="What the project does")
    stack: str | None = Field(default=None, description="Tech stack: python, vite, node, html")
    install: str | None = Field(default=None, description="Install command (e.g. 'npm install', 'uv sync')")
    start: str | None = Field(default=None, description="Start command (e.g. 'node server.js'). Null = can't start.")
    source: str | None = Field(default=None, description="Origin wolt if cloned/forked, null if created locally")
    keeper: str = Field(description="Owning wolt name")
    emoji: str = Field(default_factory=random_emoji, description="Project emoji for display")

    def can_start(self) -> bool:
        """Project can only start if it has a start command."""
        return self.start is not None


def load_project(project_dir: str | Path) -> WoltspaceProject | None:
    """Load a woltspace.json from a project directory. Returns None if missing or invalid."""
    manifest = Path(project_dir) / MANIFEST
    if not manifest.exists():
        return None
    try:
        data = json.loads(manifest.read_text())
        return WoltspaceProject(**data)
    except (json.JSONDecodeError, Exception):
        return None


def discover_projects() -> list[WoltspaceProject]:
    """Scan all wolts for projects with woltspace.json manifests."""
    projects = []
    for manifest in sorted(WOLTS_DIR.glob("*/wolt/projects/*/" + MANIFEST)):
        project = load_project(manifest.parent)
        if project:
            projects.append(project)
    return projects
