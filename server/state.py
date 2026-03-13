"""State management — viewport, views history, bot log, status."""

import json
import time
from pathlib import Path

from .config import STATE_DIR, VIEWS_HISTORY_FILE, STATUS_FILE, BOT_LOG_DIR, BOT_LOG_FILE


def ensure_state_dir():
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def sanitize_session(name: str) -> str:
    import re
    clean = re.sub(r"[^a-zA-Z0-9_-]", "", name or "")[:64]
    return clean or "main"


def current_url_file(session: str) -> Path:
    return STATE_DIR / f"current-url-{sanitize_session(session)}.json"


def redirect_file(session: str) -> Path:
    return STATE_DIR / f"redirect-{sanitize_session(session)}.json"


def get_current_url(session: str = "main") -> str | None:
    f = current_url_file(session)
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text()).get("url")
    except Exception:
        return None


def set_current_url(url: str, session: str = "main", port: int = 3000):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    safe = sanitize_session(session)
    current_url_file(safe).write_text(
        json.dumps({"url": url, "port": port, "updated": int(time.time() * 1000)})
    )
    print(f"[current:{safe}] → {url}")


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
