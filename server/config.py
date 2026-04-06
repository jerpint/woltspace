"""Shared configuration — paths, env, constants."""

import os
from pathlib import Path

# --- Directories ---

WOLTSPACE_DIR = Path(__file__).resolve().parent.parent  # /workspace/woltspace
WOLT_DIR = Path(os.environ.get("WOLT_DIR", str(WOLTSPACE_DIR)))
WOLTS_DIR = Path(os.environ.get("WOLTS_DIR", str(WOLT_DIR.parent)))
WOLT_NAME = os.environ.get("WOLT_NAME", "wolt")

SITE_DIR = WOLT_DIR / "wolt" / "site"
APPS_DIR = WOLT_DIR / "wolt" / "apps"
SPARKS_DIR = WOLT_DIR / "wolt" / "sparks"
PUBLIC_DIR = WOLTSPACE_DIR / "public"

# Per-wolt state: wolts/{wolt}/.state/
STATE_DIR = WOLTS_DIR / WOLT_NAME / ".state"
SHARES_DIR = STATE_DIR / "shares"

# Global state: wolts/.space/
SPACE_DIR = WOLTS_DIR / ".space"
SPACE_PLATFORM_DIR = SPACE_DIR / "platform"
SPACE_LOGS_DIR = SPACE_DIR / "logs"

# Session registry — per-wolt; legacy dir kept for reference only
SESSION_REGISTRY_DIR = WOLTS_DIR / WOLT_NAME / ".state" / "sessions"

PORT = int(os.environ.get("PORT", "7777"))
TUI_PORT = int(os.environ.get("TUI_PORT", "3001"))

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


def get_env(key: str) -> str:
    """Get env var, falling back to .env file."""
    return os.environ.get(key) or load_dotenv().get(key, "")
