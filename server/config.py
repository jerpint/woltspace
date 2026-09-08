"""Shared configuration — paths, env, constants."""

import os
import sys
from pathlib import Path

# --- Directories ---

WOLTSPACE_DIR = Path(
    os.environ.get("WOLTSPACE_DIR", Path(__file__).resolve().parent.parent)
)

# The env namespace helper lives in the shared runtime tree, and this module is
# imported before `app.py` puts that tree on the path. Same insert, done early
# enough that the first thing to read the environment reads it through the
# helper.
#
# Imported under a distinct name on purpose. This module also owns
# `dotenv_env`, which answers a different question — "what did the human write
# in the wolt's .env" — and the two must never be mistaken for each other: the
# helper is the only thing that knows the legacy env names, and a second
# definition called `get_env` would quietly rebind it for every importer.
_runtime_lib = str(WOLTSPACE_DIR / "container" / "lib")
if _runtime_lib not in sys.path:
    sys.path.insert(0, _runtime_lib)
from env_compat import get_env as resolve_env  # noqa: E402

WOLT_DIR = Path(resolve_env("WOLTSPACE_WOLT_DIR", str(WOLTSPACE_DIR)))
WOLTS_DIR = Path(resolve_env("WOLTSPACE_WOLTS_DIR", str(WOLT_DIR.parent)))
WOLT_NAME = resolve_env("WOLTSPACE_WOLT_NAME", "")

# The per-wolt HOME the container image builds. Containers are the only
# isolation mode that owns a home outright, so harness credentials live at a
# fixed path rather than wherever $HOME happens to point.
CONTAINER_HOME = Path(os.environ.get("WOLTSPACE_CONTAINER_HOME", "/home/node"))

SITE_DIR = WOLT_DIR / "wolt" / "site"
APPS_DIR = WOLT_DIR / "wolt" / "apps"
SPARKS_DIR = WOLT_DIR / "wolt" / "sparks"
PUBLIC_DIR = WOLTSPACE_DIR / "public"

# Per-wolt state: wolts/{wolt}/.state/
# When no wolt exists yet (onboarding), use global .space/ for state
if WOLT_NAME:
    STATE_DIR = WOLTS_DIR / WOLT_NAME / ".state"
    SESSION_REGISTRY_DIR = STATE_DIR / "sessions"
else:
    STATE_DIR = WOLTS_DIR / ".space" / "platform"
    SESSION_REGISTRY_DIR = STATE_DIR / "sessions"
SHARES_DIR = STATE_DIR / "shares"

# Global state: wolts/.space/
SPACE_DIR = WOLTS_DIR / ".space"
SPACE_PLATFORM_DIR = SPACE_DIR / "platform"
SPACE_LOGS_DIR = SPACE_DIR / "logs"

PORT = int(os.environ.get("PORT", "7777"))
# `woltspace serve` exports TUI_PORT from the connector plan before this
# module is imported; the fallback only covers a server started by hand, and
# has to derive the port the same way the plan does or the /tui proxy dials a
# bridge that is not there.
TUI_PORT = int(os.environ.get("TUI_PORT") or PORT + 1)

# --- State files ---

TOOL_REGISTRY_FILE = STATE_DIR / "tool-registry.json"
VIEWS_HISTORY_FILE = STATE_DIR / "views-history.jsonl"
BOT_LOG_DIR = SPACE_LOGS_DIR
BOT_LOG_FILE = BOT_LOG_DIR / "bot.jsonl"

# --- Notify sentinel ---

DEN_REPLY_FOOTER = "\n↩️ reply to this message to talk to this session directly"

# --- MIME types ---

MIME_TYPES = {
    ".html": "text/html",
    ".css": "text/css",
    ".js": "text/javascript",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".xml": "application/xml",
    ".txt": "text/plain",
    ".pub": "text/plain",
}

APP_MIME_TYPES = {
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".ico": "image/x-icon",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mp3": "audio/mpeg",
    ".wasm": "application/wasm",
    ".map": "application/json",
}


def load_dotenv() -> dict[str, str]:
    """Load .env file from WOLT_DIR."""
    from dotenv import dotenv_values
    env_file = WOLT_DIR / ".env"
    if not env_file.exists():
        return {}
    return {k: v for k, v in dotenv_values(env_file).items() if v is not None}


def dotenv_env(key: str) -> str:
    """A credential or setting, from the process env or the wolt's `.env`.

    Not the namespace resolver — see `resolve_env` at the top of this module.
    Everything read through here belongs to somebody else (a chat platform, a
    cloud provider, a model host) and is spelled the way its owner spells it,
    so there is no legacy woltspace name to fall back to.
    """
    return os.environ.get(key) or load_dotenv().get(key, "")
