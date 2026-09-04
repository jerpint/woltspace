"""State management — viewport, views history, bot log, status."""

import json
import sys
import time
from pathlib import Path

from .config import (
    STATE_DIR,
    VIEWS_HISTORY_FILE,
    BOT_LOG_DIR,
    BOT_LOG_FILE,
    WOLTS_DIR,
    WOLTSPACE_DIR,
)

# Import session registry from container/lib
_lib_path = WOLTSPACE_DIR / "container" / "lib"
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

def _is_onboarding() -> bool:
    """True when Claude Code hasn't been authenticated yet."""
    return not Path("/home/node/.claude/.credentials.json").exists()


def get_current_url(session: str = "main") -> str | None:
    reg = SessionRegistry(WOLTS_DIR)
    data = reg.get(sanitize_session(session), check_alive=False)
    if data:
        return data.get("viewport_url") or None
    # No session found — show onboard page if not authenticated
    if _is_onboarding():
        return "/onboard"
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
        if _is_onboarding():
            return {"url": "/onboard", "updated": 0}
        return {"url": None, "updated": 0}
    meta = {
        "url": data.get("viewport_url") or None,
        "port": data.get("viewport_port", 7777),
        "updated": data.get("viewport_updated", 0),
    }
    # If viewing an app, include its tunnel_url for remote access
    vp_url = meta["url"] or ""
    if meta["port"] != 7777 or vp_url.startswith("/app/"):
        import re
        app_match = re.match(r"^/app/([^/]+)", vp_url)
        if app_match:
            try:
                from apps import running_apps
                running = {r["name"]: r for r in running_apps()}
                run_state = running.get(app_match.group(1))
                if run_state and run_state.get("tunnel_url"):
                    meta["tunnel_url"] = run_state["tunnel_url"]
            except Exception:
                pass
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

