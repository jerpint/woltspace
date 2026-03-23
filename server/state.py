"""State management — viewport, views history, bot log, status."""

import json
import sys
import time
from pathlib import Path

from .config import (
    STATE_DIR,
    VIEWS_HISTORY_FILE,
    STATUS_FILE,
    BOT_LOG_DIR,
    BOT_LOG_FILE,
    WOLTS_DIR,
)

# Import session registry from container/lib
_lib_path = Path(__file__).resolve().parent.parent / "container" / "lib"
if str(_lib_path) not in sys.path:
    sys.path.insert(0, str(_lib_path))

from sessions import SessionRegistry  # noqa: E402


def ensure_state_dir():
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def sanitize_session(name: str) -> str:
    import re
    clean = re.sub(r"[^a-zA-Z0-9_-]", "", name or "")[:64]
    return clean or "main"


# ---------------------------------------------------------------------------
# Viewport — stored in session JSON, not in separate files
# ---------------------------------------------------------------------------

def get_current_url(session: str = "main") -> str | None:
    reg = SessionRegistry(WOLTS_DIR)
    data = reg.get(sanitize_session(session), check_alive=False)
    if data:
        return data.get("viewport_url") or None
    return None


def set_current_url(url: str, session: str = "main", port: int = 7777):
    reg = SessionRegistry(WOLTS_DIR)
    name = sanitize_session(session)
    reg.set_viewport(name, url, port=port)
    print(f"[current:{name}] → {url}")


def get_current_meta(session: str = "main") -> dict:
    """Get viewport metadata dict — url, port, updated, and any pending redirect."""
    reg = SessionRegistry(WOLTS_DIR)
    name = sanitize_session(session)
    data = reg.get(name, check_alive=False)
    if not data:
        return {"url": None, "updated": 0}
    meta = {
        "url": data.get("viewport_url") or None,
        "port": data.get("viewport_port", 7777),
        "updated": data.get("viewport_updated", 0),
    }
    # Check for pending redirect and clear it atomically
    redirect = reg.clear_redirect(name)
    if redirect:
        meta["redirect"] = redirect
    return meta


# ---------------------------------------------------------------------------
# Views history
# ---------------------------------------------------------------------------

def derive_title(url: str) -> str:
    from .config import SPARKS_DIR

    if url in ("/", "/index.html"):
        return "home"
    if url.startswith("/history/"):
        spark_id = url[len("/history/"):]
        try:
            data = json.loads((SPARKS_DIR / f"{spark_id}.json").read_text())
            return data.get("title", spark_id)
        except Exception:
            return spark_id
    name = url.split("/")[-1].replace(".html", "").replace("-", " ")
    return name or url


def log_view(url: str, title: str | None = None):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        entry = json.dumps({"url": url, "title": title or derive_title(url), "t": int(time.time() * 1000)})
        with open(VIEWS_HISTORY_FILE, "a") as f:
            f.write(entry + "\n")
    except Exception:
        pass


def read_views_history(n: int = 100) -> list[dict]:
    if not VIEWS_HISTORY_FILE.exists():
        return []
    try:
        lines = VIEWS_HISTORY_FILE.read_text().strip().splitlines()
        entries = []
        for line in lines:
            try:
                entries.append(json.loads(line))
            except Exception:
                pass
        return list(reversed(entries[-n:]))
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Bot log
# ---------------------------------------------------------------------------

def bot_log(event: str, data: dict):
    try:
        BOT_LOG_DIR.mkdir(parents=True, exist_ok=True)
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        entry = json.dumps({"ts": ts, "event": event, **data})
        with open(BOT_LOG_FILE, "a") as f:
            f.write(entry + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def read_status() -> dict:
    if not STATUS_FILE.exists():
        return {}
    try:
        return json.loads(STATUS_FILE.read_text())
    except Exception:
        return {}


def write_status(patch: dict):
    try:
        cur = read_status()
        cur.update(patch)
        cur["updatedAt"] = int(time.time() * 1000)
        STATUS_FILE.write_text(json.dumps(cur))
    except Exception:
        pass
