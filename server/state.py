"""State management — viewport, views history, bot log, status."""

import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
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
from harnesses import HARNESSES  # noqa: E402


DEFAULT_STARTER_SEED = (
    "https://github.com/jerpint/woltspace-starter-lodge.git"
    "@7e553b072bf6be18369f58dac62850b74d55d227"
)
_starter_seed_lock = threading.Lock()


def ensure_state_dir():
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def sanitize_session(name: str) -> str:
    import re
    clean = re.sub(r"[^a-zA-Z0-9_-]", "", name or "")[:64]
    return clean or "main"


# ---------------------------------------------------------------------------
# Viewport — stored in session JSON, not in separate files
# ---------------------------------------------------------------------------

def has_user_created_wolt() -> bool:
    """Whether this lodge contains a user-owned wolt.

    Future bundled wolts declare ``origin: starter`` and do not satisfy the
    "create your first wolt" milestone. Existing wolts predate this field and
    therefore count as user-owned, which keeps upgrades out of first-run UI.
    """
    try:
        configs = WOLTS_DIR.glob("*/wolt/wolt.json")
        for path in configs:
            try:
                if json.loads(path.read_text()).get("origin") != "starter":
                    return True
            except (json.JSONDecodeError, OSError):
                continue
    except OSError:
        pass
    return False


def _lodge_config() -> dict:
    try:
        data = json.loads((WOLTS_DIR / "woltspace.json").read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_lodge_config(config: dict) -> None:
    path = WOLTS_DIR / "woltspace.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(config, indent=2) + "\n")
    tmp.rename(path)


def _has_any_wolt() -> bool:
    try:
        return any(WOLTS_DIR.glob("*/wolt/wolt.json"))
    except OSError:
        return False


def has_selected_default_harness() -> bool:
    """Whether the owner explicitly answered the first-open harness prompt."""
    return _lodge_config().get("onboarding", {}).get("harness_selected") is True


def select_onboarding_harness(name: str) -> None:
    """Persist the default harness and first-run completion in one write.

    The API validates ``name`` against the harness registry before calling.
    Keeping this write beside the reader makes first-open state independent of
    harness implementation details and authentication.
    """
    config = _lodge_config()
    config.setdefault("harness", {})["default"] = name
    config.setdefault("onboarding", {})["harness_selected"] = True
    _write_lodge_config(config)


def apply_default_harness_from_env(installer=None) -> bool:
    """Complete first-run from an installer's harness hint, at most once.

    Returning ``True`` means this call made the first-run choice. Existing
    owner choices always win, and invalid hints leave the browser prompt in
    place.
    """
    if has_selected_default_harness():
        return False
    name = (os.environ.get("WOLTSPACE_DEFAULT_HARNESS") or "").strip()
    if not name:
        return False
    if name not in HARNESSES:
        registered = ", ".join(sorted(HARNESSES))
        print(
            f"[onboarding] ignoring invalid WOLTSPACE_DEFAULT_HARNESS={name!r}; "
            f"registered harnesses: {registered}",
            file=sys.stderr,
        )
        return False
    select_onboarding_harness(name)
    install_starter_seed(installer=installer)
    print(f"[onboarding] selected default harness from environment: {name}")
    return True


def install_starter_seed(installer=None) -> dict:
    """Attempt the configured starter once, without making onboarding fragile."""
    with _starter_seed_lock:
        config = _lodge_config()
        existing = config.get("starter_seed")
        if isinstance(existing, dict):
            return existing

        configured_source = os.environ.get("WOLTSPACE_STARTER_SEED")
        source = (
            DEFAULT_STARTER_SEED if configured_source is None else configured_source
        ).strip()
        installed_at = datetime.now(timezone.utc).isoformat()
        if _has_any_wolt():
            record = {
                "source": source,
                "installed_at": installed_at,
                "result": {"state": "skipped", "reason": "lodge already has wolts"},
            }
            config["starter_seed"] = record
            _write_lodge_config(config)
            return record
        if not source or source.lower() == "none":
            record = {
                "source": source,
                "installed_at": installed_at,
                "result": {"state": "skipped", "reason": "starter seed disabled"},
            }
            config["starter_seed"] = record
            _write_lodge_config(config)
            return record

        # Claim the one attempt before touching the source. A crash cannot make
        # a restart silently repeat a partially completed import.
        record = {
            "source": source,
            "installed_at": installed_at,
            "result": {"state": "installing"},
        }
        config["starter_seed"] = record
        _write_lodge_config(config)
        try:
            if installer is None:
                from woltspace.seed import install_seed
                installer = install_seed
            details = installer(
                source=source,
                wolts_dir=WOLTS_DIR,
                install_root=WOLTSPACE_DIR,
            )
            record["result"] = {"state": "installed", "details": details}
        except Exception as exc:
            record["result"] = {"state": "failed", "error": str(exc)}
            print(f"[starter-seed] install failed: {exc}", file=sys.stderr)
        config = _lodge_config()
        config["starter_seed"] = record
        _write_lodge_config(config)
        return record


def starter_seed_status() -> dict:
    record = _lodge_config().get("starter_seed")
    if not isinstance(record, dict):
        return {"state": "pending"}
    result = record.get("result")
    if not isinstance(result, dict):
        return {"state": "failed", "error": "invalid starter seed record"}
    status = {"state": result.get("state", "failed")}
    if result.get("error"):
        status["error"] = result["error"]
    if result.get("reason"):
        status["reason"] = result["reason"]
    return status


def onboarding_status() -> dict:
    """Small, durable first-run state; harness authentication is unrelated."""
    has_user_wolt = has_user_created_wolt()
    selected = has_selected_default_harness()
    return {
        # Existing lodges without the new marker migrate safely: their
        # user-owned wolt proves setup already happened.
        "needs_harness_choice": not selected and not has_user_wolt,
        "harness_selected": selected,
        "has_user_wolt": has_user_wolt,
        "starter": starter_seed_status(),
    }


def _is_onboarding() -> bool:
    return onboarding_status()["needs_harness_choice"]


def get_current_url(session: str = "main") -> str | None:
    reg = SessionRegistry(WOLTS_DIR)
    data = reg.get(sanitize_session(session), check_alive=False)
    if data:
        return data.get("viewport_url") or None
    # No session found — show the lodge, whose home owns the first-run picker.
    if _is_onboarding():
        return "/"
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
            return {"url": "/", "updated": 0}
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
