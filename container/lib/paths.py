"""
Central path helpers for the wolt-centric state model.

Two scopes:
  - Per-wolt: wolts/{wolt}/.state/  (sessions, site, wolf, chat)
  - Global:   wolts/.space/         (platform, apps, logs)

Usage:
    from paths import wolt_state_dir, wolt_sessions_dir, space_dir
"""

from __future__ import annotations

import os
from pathlib import Path

WOLTS_DIR = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))


# ---------------------------------------------------------------------------
# Per-wolt state
# ---------------------------------------------------------------------------

def wolt_state_dir(wolt: str, wolts_dir: Path = None) -> Path:
    """Per-wolt state root: wolts/{wolt}/.state/"""
    return (wolts_dir or WOLTS_DIR) / wolt / ".state"


def wolt_sessions_dir(wolt: str, wolts_dir: Path = None) -> Path:
    """Per-wolt sessions: wolts/{wolt}/.state/sessions/"""
    return wolt_state_dir(wolt, wolts_dir) / "sessions"


def wolt_site_state_file(wolt: str, wolts_dir: Path = None) -> Path:
    """Per-wolt site state: wolts/{wolt}/.state/site.json"""
    return wolt_state_dir(wolt, wolts_dir) / "site.json"


def wolt_wolf_state_dir(wolt: str, wolts_dir: Path = None) -> Path:
    """Per-wolt wolf/cron state: wolts/{wolt}/.state/wolf/"""
    return wolt_state_dir(wolt, wolts_dir) / "wolf"


def space_wolf_dir(wolts_dir: Path = None) -> Path:
    """Global wolf scheduler state: wolts/.space/wolf/"""
    return space_dir(wolts_dir) / "wolf"


def wolt_chat_dir(wolt: str, wolts_dir: Path = None) -> Path:
    """Per-wolt chat history: wolts/{wolt}/.state/chat/"""
    return wolt_state_dir(wolt, wolts_dir) / "chat"


def wolt_uploads_dir(wolt: str, wolts_dir: Path = None) -> Path:
    """Per-wolt file uploads: wolts/{wolt}/.state/uploads/"""
    return wolt_state_dir(wolt, wolts_dir) / "uploads"


def wolt_sessions_log(wolt: str, wolts_dir: Path = None) -> Path:
    """Per-wolt session summaries: wolts/{wolt}/.state/sessions.jsonl"""
    return wolt_state_dir(wolt, wolts_dir) / "sessions.jsonl"


# ---------------------------------------------------------------------------
# Global platform state
# ---------------------------------------------------------------------------

def space_dir(wolts_dir: Path = None) -> Path:
    """Global state root: wolts/.space/"""
    return (wolts_dir or WOLTS_DIR) / ".space"


def space_platform_dir(wolts_dir: Path = None) -> Path:
    """Platform state: wolts/.space/platform/"""
    return space_dir(wolts_dir) / "platform"


def space_apps_dir(wolts_dir: Path = None) -> Path:
    """App running state: wolts/.space/apps/"""
    return space_dir(wolts_dir) / "apps"


# Backwards compat alias
space_projects_dir = space_apps_dir


def space_logs_dir(wolts_dir: Path = None) -> Path:
    """Logs: wolts/.space/logs/"""
    return space_dir(wolts_dir) / "logs"


# ---------------------------------------------------------------------------
# Specific files
# ---------------------------------------------------------------------------

def tunnel_url_file(wolts_dir: Path = None) -> Path:
    """Tunnel URL file: wolts/.space/platform/tunnel-url"""
    return space_platform_dir(wolts_dir) / "tunnel-url"


def platform_version_file(wolts_dir: Path = None) -> Path:
    """Platform version: wolts/.space/platform/woltspace-version"""
    return space_platform_dir(wolts_dir) / "woltspace-version"


def platform_branch_file(wolts_dir: Path = None) -> Path:
    """Platform branch: wolts/.space/platform/woltspace-branch"""
    return space_platform_dir(wolts_dir) / "woltspace-branch"


def space_vulture_dir(wolts_dir: Path = None) -> Path:
    """Vulture reaper state: wolts/.space/vulture/"""
    return space_dir(wolts_dir) / "vulture"


def space_task_results_dir(wolts_dir: Path = None) -> Path:
    """Task results: wolts/.space/task-results/"""
    return space_dir(wolts_dir) / "task-results"
