"""Shared configuration — paths, env, constants."""

import os
from pathlib import Path

# --- Directories ---

WOLTSPACE_DIR = Path(__file__).resolve().parent.parent  # /workspace/woltspace
WOLT_DIR = Path(os.environ.get("WOLT_DIR", str(WOLTSPACE_DIR)))
WOLTS_DIR = Path(os.environ.get("WOLTS_DIR", str(WOLT_DIR.parent)))

SITE_DIR = WOLT_DIR / "wolt" / "site"
PROJECTS_DIR = WOLT_DIR / "wolt" / "projects"
APPS_DIR = WOLT_DIR / "wolt" / "apps"
SPARKS_DIR = WOLT_DIR / "wolt" / "sparks"
PUBLIC_DIR = WOLTSPACE_DIR / "public"
STATE_DIR = WOLT_DIR / ".state"
WOLTS_STATE_DIR = WOLTS_DIR / ".state"
SESSION_REGISTRY_DIR = WOLTS_STATE_DIR / "registry"
SHARES_DIR = STATE_DIR / "shares"

WOLT_NAME = os.environ.get("WOLT_NAME", "wolt")
PORT = int(os.environ.get("PORT", "7777"))
TUI_PORT = int(os.environ.get("TUI_PORT", "3001"))

# --- State files ---

TOOL_REGISTRY_FILE = STATE_DIR / "tool-registry.json"
VIEWS_HISTORY_FILE = STATE_DIR / "views-history.jsonl"
STATUS_FILE = STATE_DIR / "status.json"
BOT_LOG_DIR = STATE_DIR / "bot-debug"
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
    env_file = WOLT_DIR / ".env"
    if not env_file.exists():
        return {}
    result = {}
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        eq = line.find("=")
        if eq > 0:
            result[line[:eq].strip()] = line[eq + 1 :].strip()
    return result


def get_env(key: str) -> str:
    """Get env var, falling back to .env file."""
    return os.environ.get(key) or load_dotenv().get(key, "")
