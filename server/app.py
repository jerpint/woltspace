"""Woltspace server — FastAPI replacement for server.js.

All endpoints except /tui WebSocket (which stays in Node via tui-service.js).
"""

import json
import os
import re
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)

from . import tools as tool_registry
from .config import (
    APP_MIME_TYPES,
    DEN_REPLY_FOOTER,
    MIME_TYPES,
    PORT,
    PROJECTS_DIR,
    PUBLIC_DIR,
    SESSION_REGISTRY_DIR,
    SHARES_DIR,
    SITE_DIR,
    SPARKS_DIR,
    STATE_DIR,
    STATUS_FILE,
    TUI_PORT,
    WOLT_DIR,
    WOLT_NAME,
    WOLTS_DIR,
    WOLTS_STATE_DIR,
    get_env,
    load_dotenv,
)
from .notify import send_notification
from .sparks import get_spark_with_chain, list_sparks

# Session spawning — shared with bot
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))
from sessions import start_session
from projects import (
    WoltspaceProject,
    discover_projects,
    get_project,
    project_dir,
    running_projects,
    start_project,
    stop_project,
)
from sites import get_site_state, running_sites, site_dir, start_site, stop_site
from .state import (
    bot_log,
    current_url_file,
    get_current_url,
    log_view,
    read_status,
    read_views_history,
    redirect_file,
    sanitize_session,
    set_current_url,
    write_status,
)

# --- Live reload ---

_livereload_clients: set[WebSocket] = set()

LIVERELOAD_SCRIPT = '<script>(function(){var p=location.protocol==="https:"?"wss:":"ws:";function connect(){var ws=new WebSocket(p+"//"+location.host+"/livereload");ws.onmessage=function(){location.reload()};ws.onclose=function(){setTimeout(connect,3000)}}connect()})()</script>'


async def _broadcast_reload():
    dead = []
    for ws in _livereload_clients:
        try:
            await ws.send_text("reload")
        except Exception:
            dead.append(ws)
    for ws in dead:
        _livereload_clients.discard(ws)


def _start_file_watcher():
    """Watch wolt/site/ for changes and broadcast reload."""
    import asyncio
    import threading

    from watchfiles import watch

    def _watch():
        loop = None
        for _changes in watch(str(SITE_DIR)):
            if loop is None:
                loop = asyncio.get_event_loop()
            loop.call_soon_threadsafe(asyncio.ensure_future, _broadcast_reload())

    if SITE_DIR.exists():
        t = threading.Thread(target=_watch, daemon=True)
        t.start()
        print(f"[livereload] watching {SITE_DIR}")


# Digest cron removed — the wolf creature owns all scheduling via wolt/wolf.json.
# See creatures/wolf.py.


# --- Tool GC ---
# jerpint: what is this lol

def _start_tool_gc():
    import threading

    def _gc_loop():
        while True:
            time.sleep(30)
            tool_registry.gc()

    t = threading.Thread(target=_gc_loop, daemon=True)
    t.start()


# --- Lifespan ---
# jerpint: also this?

@asynccontextmanager
async def lifespan(app: FastAPI):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tool_registry.restore()
    _start_file_watcher()
    _start_tool_gc()
    print(f"""
  woltspace server (python) · http://localhost:{PORT}
  wolt: {WOLT_NAME}
  tui proxy → localhost:{TUI_PORT}
    """)
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


# --- Middleware: CORS ---

# jerpint: nice well need some proper auth layers to separate /public/ from the rest

@app.middleware("http")
async def cors_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS, DELETE"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response

@app.options("/{path:path}")
async def options_handler():
    return Response(status_code=204)


# --- Helpers ---

def _shell_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _project_not_found(name: str) -> str:
    """Styled 404 page for a project that doesn't exist."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name} — not found</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace;
    background: #1a1a1a; color: #c8b89a;
    display: flex; align-items: center; justify-content: center;
    min-height: 100vh; padding: 2rem;
  }}
  .card {{
    max-width: 420px; width: 100%; text-align: center;
    border: 1px solid #3a3a2a; border-radius: 12px;
    padding: 2.5rem 2rem; background: #222218;
  }}
  .emoji {{ font-size: 3rem; margin-bottom: 1rem; }}
  h1 {{ font-size: 1.3rem; color: #e8d8b8; margin-bottom: 0.5rem; }}
  .desc {{ font-size: 0.85rem; color: #8a8060; margin-bottom: 1.5rem; }}
  .hint {{ font-size: 0.75rem; color: #5a5a4a; }}
</style>
</head>
<body>
<div class="card">
  <div class="emoji">🌲</div>
  <h1>{name}</h1>
  <p class="desc">This project doesn't exist yet.</p>
  <p class="hint">Ask your wolt to create it, or check the name.</p>
</div>
</body>
</html>"""


def _project_placeholder(name: str, project: "WoltspaceProject | None" = None) -> str:
    """Styled placeholder page for a project that has no servable content."""
    emoji = project.emoji if project else "📦"
    desc = project.description if project and project.description else "No description yet"
    keeper = project.keeper if project else "unknown"
    start_cmd = project.start if project and project.start else None
    status = "Press start to start your project" if start_cmd else "No start command configured"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name} — off</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace;
    background: #1a1a1a; color: #c8b89a;
    display: flex; align-items: center; justify-content: center;
    min-height: 100vh; padding: 2rem;
  }}
  .card {{
    max-width: 420px; width: 100%; text-align: center;
    border: 1px solid #3a3a2a; border-radius: 12px;
    padding: 2.5rem 2rem; background: #222218;
  }}
  .emoji {{ font-size: 3rem; margin-bottom: 1rem; }}
  h1 {{ font-size: 1.3rem; color: #e8d8b8; margin-bottom: 0.5rem; }}
  .desc {{ font-size: 0.85rem; color: #8a8060; margin-bottom: 1.5rem; }}
  .status {{
    font-size: 0.8rem; color: #6a7a4a;
    border-top: 1px solid #3a3a2a; padding-top: 1rem;
  }}
  .keeper {{ font-size: 0.75rem; color: #5a5a4a; margin-top: 0.8rem; }}
</style>
</head>
<body>
<div class="card">
  <div class="emoji">{emoji}</div>
  <h1>{name}</h1>
  <p class="desc">{desc}</p>
  <p class="status">{status}</p>
  <p class="keeper">keeper: {keeper}</p>
</div>
</body>
</html>"""


# jerpint: what trickery is this well need to revise all these hacks
def _inject_livereload(html: str) -> str:
    if "</body>" in html:
        return html.replace("</body>", LIVERELOAD_SCRIPT + "</body>")
    return html + LIVERELOAD_SCRIPT


# jerpint: what do we serve as static vs not?
# overall i think it might be simpler to think of each serve as a single service
# tbd how memory intensive this is, but e.g. each session is powered by its own single python --serve index.html with
# its own port so theyre really easy to just swap out and share. we shouldnt have different routes for different types
# maybe static pages an have something less overkill, but it would be nice to have a unified way of doing things
async def _serve_static(url_path: str, request: Request | None = None) -> Response | None:
    """Serve from wolt/site/."""
    full_path = SITE_DIR / url_path.lstrip("/")
    if not full_path.exists() or not full_path.is_file():
        return None
    # Security: stay inside SITE_DIR
    try:
        full_path.resolve().relative_to(SITE_DIR.resolve())
    except ValueError:
        return None
    content = full_path.read_bytes()
    ext = full_path.suffix
    is_iframe = request and request.headers.get("sec-fetch-dest") == "iframe"
    if ext == ".html" and not is_iframe:
        html = _inject_livereload(content.decode())
        return HTMLResponse(html, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    mime = MIME_TYPES.get(ext, "application/octet-stream")
    return Response(content, media_type=mime, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


# jerpint: will have to look into how this works and security around it
async def _serve_platform_file(filename: str) -> Response | None:
    """Serve from public/ (platform UI)."""
    path = PUBLIC_DIR / filename
    if not path.exists():
        return None
    content = path.read_text()
    return HTMLResponse(content, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


# ============================================================
# ROUTES
# ============================================================

# jerpint: good idea, but eventually should be tied to version of woltspace package not just plaintext
@app.get("/version")
async def version():
    return PlainTextResponse("woltspace-v1")


# --- Viewport control ---

# jerpint: not sure if a concept of current is useful, but could be, e.g. current vs last
@app.post("/current")
async def post_current(request: Request):
    session = sanitize_session(request.query_params.get("session", "main"))
    body = await request.json()
    url = body.get("url")
    if url:
        set_current_url(url, session, body.get("port", 7777))
        log_view(url, body.get("title"))
    return {"url": get_current_url(session)}


@app.get("/current")
async def get_current(request: Request):
    session = sanitize_session(request.query_params.get("session", "main"))
    url = get_current_url(session)
    if url:
        return RedirectResponse(url, status_code=302)
    return Response(status_code=204)


@app.get("/current/meta")
async def get_current_meta(request: Request):
    session = sanitize_session(request.query_params.get("session", "main"))
    f = current_url_file(session)
    if f.exists():
        data = json.loads(f.read_text())
    else:
        data = {"url": None, "updated": 0}
    # Check for pending redirect
    rf = redirect_file(session)
    if rf.exists():
        try:
            rdata = json.loads(rf.read_text())
            data["redirect"] = rdata["to"]
            rf.unlink()
        except Exception:
            pass
    return data


# jerpint: why this?
@app.post("/sessions/redirect")
async def post_session_redirect(request: Request):
    body = await request.json()
    from_s = body.get("from")
    to_s = body.get("to")
    if not from_s or not to_s:
        return JSONResponse({"error": "from and to required"}, status_code=400)
    safe_from = sanitize_session(from_s)
    safe_to = sanitize_session(to_s)
    redirect_file(safe_from).write_text(json.dumps({"from": safe_from, "to": safe_to, "t": int(time.time() * 1000)}))
    print(f"[redirect] {safe_from} → {safe_to}")
    return {"ok": True}


# --- Status ---

@app.get("/status")
async def get_status():
    status = read_status()
    latest_spark = None
    try:
        files = sorted(
            [f for f in SPARKS_DIR.iterdir() if f.name.endswith(".json")],
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        if files:
            d = json.loads(files[0].read_text())
            latest_spark = {"id": d["id"], "title": d.get("title"), "timestamp": d.get("timestamp"), "report": d.get("report")}
    except Exception:
        pass
    return {
        "wolt": WOLT_NAME,
        "digest": status.get("digest", {"state": "unknown"}),
        "currentView": get_current_url("main"),
        "latestSpark": latest_spark,
        "serverUptime": int(time.time() - _start_time),
        "updatedAt": status.get("updatedAt"),
    }


@app.get("/onboard-status")
async def onboard_status():
    env = load_dotenv()
    return {
        "wolts_dir": str(WOLTS_DIR),
        "wolt_name": WOLT_NAME,
        "has_oauth": Path("/home/node/.claude/.credentials.json").exists(),
        "has_human_name": bool(env.get("HUMAN_NAME", "").strip() and env.get("HUMAN_NAME") != "your-name"),
        "has_llm_key": bool(env.get("ANTHROPIC_API_KEY") or env.get("OPENROUTER_API_KEY")),
        "has_telegram": env.get("ENABLE_TELEGRAM_BOT") == "true" and bool(env.get("TELEGRAM_BOT_TOKEN")),
    }


@app.get("/views/history")
async def views_history():
    return read_views_history(100)


# --- Notify ---

@app.post("/notify")
async def post_notify(request: Request):
    body = await request.json()
    message = body.get("message")
    if not message:
        return JSONResponse({"error": "message required"}, status_code=400)
    session = body.get("session", "")
    try:
        result = await send_notification(session, message)
        print(f"[notify] → {result.get('adapter')} | {message[:80]}")
        bot_log("notify_sent", {"session": session, **result, "message": message})
        return {"ok": True, **result}
    except Exception as e:
        print(f"[notify] error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# --- Memory ---

@app.post("/memory/read")
async def memory_read(request: Request):
    body = await request.json()
    mem_path = body.get("path")
    if not mem_path or not isinstance(mem_path, str):
        return JSONResponse({"error": "path required"}, status_code=400)
    memory_dir = WOLT_DIR / "wolt" / "memory"
    abs_path = (memory_dir / mem_path).resolve()
    if not str(abs_path).startswith(str(memory_dir.resolve())):
        return JSONResponse({"error": "path outside memory directory"}, status_code=403)
    if not abs_path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        content = abs_path.read_text()
        return {"path": mem_path, "content": content}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# --- Session messaging ---

@app.post("/sessions/{session_id}/message")
async def session_message(session_id: str, request: Request):
    safe = sanitize_session(session_id)
    body = await request.json()
    text = body.get("text")
    if not text:
        return JSONResponse({"error": "text required"}, status_code=400)
    try:
        subprocess.run(["tmux", "send-keys", "-t", safe, "-l", text], check=True)
        subprocess.run(["tmux", "send-keys", "-t", safe, "Enter"], check=True)
        print(f"[message] → {safe}: {text[:80]}")
        return {"ok": True, "session": safe}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# --- Session spawning ---
# All session creation goes through start_session() from container/lib/sessions.py.
# Each adapter (lodge, telegram, slack) has its own route for adapter-specific params.

@app.post("/sessions/new/create")
async def session_new_create(request: Request):
    """Start a session to create a new wolt. No existing wolt needed."""
    name = f"create-wolt-{int(time.time() * 1000) % 100000}"
    work_dir = str(WOLTS_DIR)
    try:
        SESSION_REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
        import json as _json
        (SESSION_REGISTRY_DIR / f"{name}.json").write_text(_json.dumps({
            "name": name, "wolt": "", "creature": "beaver", "model": "sonnet",
            "status": "running", "created_at": int(time.time()),
            "dir": work_dir, "prompt": "/create-wolt", "adapter": "lodge",
        }, indent=2) + "\n")
        # Run claude directly — bypass run-session.sh since there's no wolt yet.
        # Set WOLT_SESSION so push-view can target the right viewport.
        cmd = f"export WOLT_SESSION={name} && claude --dangerously-skip-permissions --model sonnet '/create-wolt new'"
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", name, "-c", work_dir, "bash", "-c", cmd],
            check=True,
        )
        print(f"[sessions/create] spawned {name}")
        return {"name": name}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/sessions/new/lodge")
async def session_new_lodge(request: Request):
    """Start a session from the lodge (home page gnaw button)."""
    body = await request.json()
    wolt = body.get("wolt")
    if not wolt:
        return JSONResponse({"error": "wolt required"}, status_code=400)
    try:
        result = start_session(
            wolt=wolt,
            prompt=body.get("prompt", ""),
            creature=body.get("creature", ""),
            project=body.get("project", ""),
            routing={"adapter": "lodge"},
        )
        # Auto-start wolt site and set it as the viewport immediately
        if not body.get("project"):
            try:
                site_state = start_site(wolt)
                site_url = f"/wolt/{wolt}/site/"
                result["site_url"] = site_url
                result["site_port"] = site_state["port"]
                # Pre-load viewport so it's ready before claude boots
                set_current_url(site_url, result["name"], 7777)
            except Exception as e:
                print(f"[sites] failed to auto-start for {wolt}: {e}")
        print(f"[sessions/lodge] spawned {result['name']} for {wolt}")
        return result
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/sessions/new/telegram")
async def session_new_telegram(request: Request):
    """Start a session from Telegram."""
    body = await request.json()
    wolt = body.get("wolt")
    if not wolt:
        return JSONResponse({"error": "wolt required"}, status_code=400)
    try:
        result = start_session(
            wolt=wolt,
            prompt=body.get("prompt", ""),
            creature=body.get("creature", ""),
            project=body.get("project", ""),
            routing={
                "adapter": "telegram",
                "chat_id": body.get("chat_id", ""),
                "user_id": body.get("user_id", ""),
            },
        )
        print(f"[sessions/telegram] spawned {result['name']} for {wolt}")
        return result
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/sessions/new/slack")
async def session_new_slack(request: Request):
    """Start a session from Slack."""
    body = await request.json()
    wolt = body.get("wolt")
    if not wolt:
        return JSONResponse({"error": "wolt required"}, status_code=400)
    try:
        result = start_session(
            wolt=wolt,
            prompt=body.get("prompt", ""),
            creature=body.get("creature", ""),
            project=body.get("project", ""),
            routing={
                "adapter": "slack",
                "chat_id": body.get("channel", ""),
                "user_id": body.get("user_id", ""),
                "thread_ts": body.get("thread_ts", ""),
            },
        )
        print(f"[sessions/slack] spawned {result['name']} for {wolt}")
        return result
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# --- Sessions list ---

@app.get("/sessions")
async def list_sessions():
    tmux_sessions = set()
    try:
        raw = subprocess.check_output(
            ["tmux", "list-sessions", "-F", "#{session_name}"], text=True
        ).strip()
        tmux_sessions = set(raw.splitlines())
    except Exception:
        pass
    sessions = []
    try:
        for f in SESSION_REGISTRY_DIR.iterdir():
            if not f.name.endswith(".json"):
                continue
            try:
                data = json.loads(f.read_text())
                data["alive"] = data.get("name") in tmux_sessions
                if data["alive"]:
                    data["status"] = "running"
                elif data.get("status") == "running":
                    data["status"] = "orphaned"
                sessions.append(data)
            except Exception:
                pass
    except Exception:
        pass
    sessions.sort(key=lambda s: (0 if s.get("status") == "running" else 1, -(s.get("created_at") or 0)))
    return sessions


# --- Sparks ---
# jerpint: i think the concept of sparks will disappear, or be renamed and rethought as a concept
# wee will need session history, and within a session we might want to support versionoing (though maybe just let users
# use git for that)

@app.get("/history")
async def history():
    return await list_sparks()


@app.get("/history/{spark_id}/meta")
async def history_meta(spark_id: str):
    try:
        data = await get_spark_with_chain(spark_id)
        data.pop("html", None)
        return data
    except Exception:
        return JSONResponse({"error": "not found"}, status_code=404)


@app.get("/history/{spark_id}")
async def history_detail(spark_id: str):
    try:
        data = await get_spark_with_chain(spark_id)
        return HTMLResponse(
            data["html"],
            headers={
                "x-spark-id": data["id"],
                "x-spark-parent": data.get("parentId") or "",
                "x-spark-child": data.get("childId") or "",
                "x-spark-version": str(data["version"]),
                "x-spark-total": str(data["totalVersions"]),
            },
        )
    except Exception:
        return PlainTextResponse("spark not found", status_code=404)


# --- Wolts ---

@app.get("/wolts")
async def list_wolts():
    """List all wolts by scanning WOLTS_DIR for wolt/wolt.json files."""
    wolts = []
    if WOLTS_DIR.exists():
        for entry in sorted(WOLTS_DIR.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            wolt_json = entry / "wolt" / "wolt.json"
            if not wolt_json.exists():
                continue
            try:
                config = json.loads(wolt_json.read_text())
                wolts.append({
                    "dir": entry.name,
                    **config,
                })
            except Exception:
                pass
    return wolts


# --- Apps (deprecated — use /projects/ and /project/) ---
# Legacy /app/ routes removed. Apps are now projects with woltspace.json.
# Existing apps in wolt/apps/ need migration to wolts/projects/.


# --- Projects ---
# Centralized project management. Uses woltspace.json manifests.
# Project names are globally unique. Keeper (owning wolt) is in woltspace.json.

@app.get("/projects")
async def list_projects_api():
    """List all projects that have woltspace.json."""
    projects = discover_projects()
    running = {r["name"]: r for r in running_projects()}
    result = []
    for p in projects:
        entry = p.model_dump()
        run_state = running.get(p.name)
        entry["running"] = run_state is not None
        entry["port"] = run_state["port"] if run_state else None
        entry["url"] = f"/project/{p.name}/"
        result.append(entry)
    return result


@app.get("/projects/{name}")
async def project_detail(name: str):
    """Get a single project's manifest and running state."""
    project = get_project(name)
    if not project:
        return JSONResponse({"error": f"project {name} not found"}, status_code=404)
    running = {r["name"]: r for r in running_projects()}
    entry = project.model_dump()
    run_state = running.get(name)
    entry["running"] = run_state is not None
    entry["port"] = run_state["port"] if run_state else None
    entry["url"] = f"/project/{name}/"
    return entry


@app.post("/projects/{name}/start")
async def project_start(name: str):
    """Start a project's dev server."""
    try:
        state = start_project(name)
        print(f"[projects] started {name} on port {state['port']}")
        return state
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=409)


@app.post("/projects/{name}/stop")
async def project_stop(name: str):
    """Stop a running project."""
    was_running = stop_project(name)
    if was_running:
        print(f"[projects] stopped {name}")
        return {"ok": True, "name": name}
    return JSONResponse({"error": f"{name} is not running"}, status_code=404)


# --- Wolt Sites ---
# Each wolt has a persistent site at wolt/site/. Served via livereload.
# Sites auto-start when a session begins outside a project context.

@app.get("/sites")
async def list_sites_api():
    """List all running wolt sites."""
    return running_sites()


@app.get("/sites/{wolt_name}")
async def site_detail(wolt_name: str):
    """Get a wolt's site state."""
    state = get_site_state(wolt_name)
    sdir = site_dir(wolt_name)
    return {
        "wolt": wolt_name,
        "running": state is not None,
        "port": state["port"] if state else None,
        "url": f"/wolt/{wolt_name}/site/",
        "dir_exists": sdir.exists(),
    }


@app.post("/sites/{wolt_name}/start")
async def site_start(wolt_name: str):
    """Start a wolt's site livereload server."""
    sdir = site_dir(wolt_name)
    wolt_dir = WOLTS_DIR / wolt_name
    if not wolt_dir.exists():
        return JSONResponse({"error": f"wolt {wolt_name} not found"}, status_code=404)
    try:
        state = start_site(wolt_name)
        print(f"[sites] started {wolt_name} on port {state['port']}")
        return state
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=409)


@app.post("/sites/{wolt_name}/stop")
async def site_stop(wolt_name: str):
    """Stop a wolt's site livereload server."""
    was_running = stop_site(wolt_name)
    if was_running:
        print(f"[sites] stopped {wolt_name}")
        return {"ok": True, "wolt": wolt_name}
    return JSONResponse({"error": f"{wolt_name} site is not running"}, status_code=404)


@app.get("/wolt/{wolt_name}/site/{path:path}")
@app.get("/wolt/{wolt_name}/site")
async def serve_wolt_site(wolt_name: str, request: Request, path: str = ""):
    """Serve a wolt's site — proxy to livereload, rewrite reload script."""
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9_-]*$", wolt_name):
        return JSONResponse({"error": "invalid wolt name"}, status_code=400)

    wolt_dir = WOLTS_DIR / wolt_name
    if not wolt_dir.exists():
        return PlainTextResponse(f"Wolt {wolt_name} not found", status_code=404)

    # Auto-start site if not running
    state = get_site_state(wolt_name)
    if not state:
        try:
            state = start_site(wolt_name)
            print(f"[sites] auto-started {wolt_name} on port {state['port']}")
        except RuntimeError as e:
            return PlainTextResponse(f"Could not start site: {e}", status_code=503)

    # Proxy to livereload
    port = state["port"]
    sub_path = "/" + path if path else "/"
    target = f"http://localhost:{port}{sub_path}"
    if request.url.query:
        target += f"?{request.url.query}"

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.request(
                request.method, target,
                headers={k: v for k, v in request.headers.items() if k.lower() != "host"},
                content=await request.body(),
            )
            headers = dict(resp.headers)
            headers.pop("x-frame-options", None)
            headers.pop("content-security-policy", None)
            content = resp.content
            ct = headers.get("content-type", "")
            if "text/html" in ct:
                text = content.decode("utf-8", errors="replace")
                # Strip livereload's native script (it connects to /livereload
                # which doesn't work through our proxy)
                text = re.sub(
                    r'<script type="text/javascript">\(function\(\)\{var s=document\.createElement\("script"\).*?</script>',
                    '',
                    text,
                )
                # Inject our own reload script using a wolt-scoped WS path
                reload_script = (
                    '<script>(function(){'
                    'var p=location.protocol==="https:"?"wss:":"ws:";'
                    f'function c(){{var ws=new WebSocket(p+"//"+location.host+"/wolt/{wolt_name}/site/livereload");'
                    'ws.onmessage=function(){location.reload()};'
                    'ws.onclose=function(){setTimeout(c,3000)}}'
                    'c()})()</script>'
                )
                if '</body>' in text:
                    text = text.replace('</body>', reload_script + '</body>')
                else:
                    text += reload_script
                content = text.encode("utf-8")
                headers.pop("content-length", None)
            return Response(content, status_code=resp.status_code, headers=headers)
        except httpx.ConnectError:
            # Return a self-refreshing page that retries until livereload is ready
            return HTMLResponse(
                f'''<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  body {{ font-family: 'SF Mono', monospace; background: #2a1f14; color: #7bbf8a;
         display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; }}
  .msg {{ text-align: center; font-size: 0.82rem; letter-spacing: 0.08em; opacity: 0.8; }}
</style></head>
<body><div class="msg">{wolt_name} waking up<span class="dots">...</span></div>
<script>setTimeout(()=>location.reload(), 1000)</script>
</body></html>''',
                status_code=502,
            )


@app.websocket("/wolt/{wolt_name}/site/livereload")
async def site_livereload_ws(wolt_name: str, ws: WebSocket):
    """Watch a wolt's site dir for changes and push reload via WebSocket."""
    from watchfiles import awatch

    sdir = WOLTS_DIR / wolt_name / "wolt" / "site"
    if not sdir.exists():
        await ws.close()
        return
    await ws.accept()
    try:
        async for _changes in awatch(str(sdir)):
            try:
                await ws.send_text("reload")
            except Exception:
                break
    except WebSocketDisconnect:
        pass
    except Exception:
        pass


@app.get("/project/{proj_name}/{path:path}")
@app.get("/project/{proj_name}")
async def serve_project(proj_name: str, request: Request, path: str = ""):
    """Serve a project — proxy to running dev server, static from dist/, or direct files."""
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9_-]*$", proj_name):
        return JSONResponse({"error": "invalid project name"}, status_code=400)
    pdir = project_dir(proj_name)
    if not pdir.exists():
        return HTMLResponse(_project_not_found(proj_name), status_code=404)

    sub_path = "/" + path if path else "/"

    # Strategy 1: proxy to running dev server (managed by start_project)
    running = {r["name"]: r for r in running_projects()}
    run_state = running.get(proj_name)
    if run_state:
        port = run_state["port"]
        target = f"http://localhost:{port}{sub_path}"
        if request.url.query:
            target += f"?{request.url.query}"
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.request(
                    request.method, target,
                    headers={k: v for k, v in request.headers.items() if k.lower() != "host"},
                    content=await request.body(),
                )
                headers = dict(resp.headers)
                headers.pop("x-frame-options", None)
                headers.pop("content-security-policy", None)
                return Response(resp.content, status_code=resp.status_code, headers=headers)
            except httpx.ConnectError:
                return PlainTextResponse(f'Project "{proj_name}" not responding on port {port}', status_code=502)

    # Strategy 2: static from dist/
    dist_dir = pdir / "dist"
    if dist_dir.exists():
        candidates = [dist_dir / path, dist_dir / path / "index.html"]
        for candidate in candidates:
            resolved = candidate.resolve()
            if not str(resolved).startswith(str(dist_dir.resolve())):
                continue
            if resolved.exists() and resolved.is_file():
                ext = resolved.suffix
                mime = MIME_TYPES.get(ext) or APP_MIME_TYPES.get(ext, "application/octet-stream")
                return Response(resolved.read_bytes(), media_type=mime, headers={"Cache-Control": "no-cache"})
        return PlainTextResponse("Not found in project", status_code=404)

    # Strategy 3: serve static files directly from project root (simple HTML projects)
    candidates = [pdir / path, pdir / path / "index.html"]
    if not path:
        candidates = [pdir / "index.html"]
    for candidate in candidates:
        resolved = candidate.resolve()
        if not str(resolved).startswith(str(pdir.resolve())):
            continue
        if resolved.exists() and resolved.is_file():
            ext = resolved.suffix
            mime = MIME_TYPES.get(ext) or APP_MIME_TYPES.get(ext, "application/octet-stream")
            return Response(resolved.read_bytes(), media_type=mime, headers={"Cache-Control": "no-cache"})

    # Strategy 4: off-state placeholder — project exists but has no servable content
    project = get_project(proj_name)
    return HTMLResponse(_project_placeholder(proj_name, project))


# --- Shares ---

@app.post("/shares")
async def create_share(request: Request):
    body = await request.json()
    target_session = sanitize_session(body.get("session", "main"))
    session_file = current_url_file(target_session)
    session_data = json.loads(session_file.read_text()) if session_file.exists() else {}
    port = session_data.get("port", 7777)
    token = target_session
    SHARES_DIR.mkdir(parents=True, exist_ok=True)
    (SHARES_DIR / f"{token}.json").write_text(json.dumps({
        "session": target_session, "port": port,
        "label": body.get("label"), "created": int(time.time() * 1000), "wolt": WOLT_NAME,
    }))
    print(f"[shares] created {token} → port {port}")
    return JSONResponse({"token": token, "url": f"/public/{token}", "session": target_session, "port": port}, status_code=201)


@app.get("/shares")
async def list_shares():
    SHARES_DIR.mkdir(parents=True, exist_ok=True)
    shares = []
    for f in SHARES_DIR.iterdir():
        if not f.name.endswith(".json"):
            continue
        try:
            token = f.stem
            data = json.loads(f.read_text())
            # Liveness check
            import socket
            alive = False
            try:
                s = socket.create_connection(("localhost", data["port"]), timeout=0.5)
                s.close()
                alive = True
            except Exception:
                pass
            shares.append({"token": token, **data, "alive": alive})
        except Exception:
            pass
    return shares


# jerpint: what are these tokens? who issues them? isnt public just public?
@app.delete("/shares/{token}")
async def delete_share(token: str):
    share_file = SHARES_DIR / f"{token}.json"
    if not share_file.exists():
        return JSONResponse({"error": "share not found"}, status_code=404)
    share_file.unlink()
    print(f"[shares] revoked token {token}")
    return {"ok": True, "token": token}


@app.get("/public/{token}/{path:path}")
@app.get("/public/{token}")
async def public_proxy(token: str, request: Request, path: str = ""):
    share_file = SHARES_DIR / f"{token}.json"
    if not share_file.exists():
        return PlainTextResponse("Share link not found or revoked.", status_code=404)
    try:
        share_data = json.loads(share_file.read_text())
    except Exception:
        return PlainTextResponse("invalid share config", status_code=500)

    port = share_data["port"]
    share_session = share_data.get("session", "main")

    # No subpath → redirect to session's current viewport
    if not path:
        session_file = current_url_file(sanitize_session(share_session))
        session_data = json.loads(session_file.read_text()) if session_file.exists() else {}
        viewport = session_data.get("url", "/")
        return RedirectResponse(f"/public/{token}{viewport}", status_code=302)

    target = f"http://localhost:{port}/{path}"
    if request.url.query:
        target += f"?{request.url.query}"
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.request(
                request.method, target,
                headers={k: v for k, v in request.headers.items() if k.lower() != "host"},
                content=await request.body(),
            )
            headers = dict(resp.headers)
            headers.pop("x-frame-options", None)
            headers.pop("content-security-policy", None)
            return Response(resp.content, status_code=resp.status_code, headers=headers)
        except httpx.ConnectError:
            return PlainTextResponse(f"Service not running on port {port}.", status_code=502)


# --- Tools ---

# jerpint: this is for the telegram bots right? good idea to have them all in one place, but maybe we can have this in a
# separate /bot/ route
@app.get("/tools")
async def list_tools():
    return tool_registry.list_all()


@app.post("/tools/spawn")
async def spawn_tool(request: Request):
    body = await request.json()
    name = body.get("name")
    command = body.get("command")
    port = body.get("port")
    if not name or not command or not port:
        return JSONResponse({"error": "name, command, and port required"}, status_code=400)
    try:
        result = tool_registry.spawn(name, command, port)
        return result
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=409)


@app.api_route("/tools/{tool_name}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_tool(tool_name: str, path: str, request: Request):
    tool = tool_registry.get(tool_name)
    if not tool:
        return PlainTextResponse("tool not found", status_code=404)
    target = f"http://127.0.0.1:{tool['port']}/{path}"
    if request.url.query:
        target += f"?{request.url.query}"
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.request(
                request.method, target,
                headers={k: v for k, v in request.headers.items() if k.lower() != "host"},
                content=await request.body(),
            )
            return Response(resp.content, status_code=resp.status_code, headers=dict(resp.headers))
        except httpx.ConnectError:
            return PlainTextResponse("tool unavailable", status_code=502)


# --- WebSocket: live reload ---

@app.websocket("/livereload")
async def livereload_ws(ws: WebSocket):
    await ws.accept()
    _livereload_clients.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _livereload_clients.discard(ws)


# --- WebSocket: TUI proxy to Node service ---

@app.websocket("/tui")
async def tui_proxy(ws: WebSocket):
    """Proxy TUI WebSocket to the Node pty service on TUI_PORT."""
    import asyncio
    import websockets

    session = ws.query_params.get("session", "main")
    await ws.accept()

    try:
        async with websockets.connect(f"ws://localhost:{TUI_PORT}/tui?session={session}") as node_ws:
            async def client_to_node():
                try:
                    while True:
                        data = await ws.receive_text()
                        await node_ws.send(data)
                except WebSocketDisconnect:
                    pass

            async def node_to_client():
                try:
                    async for msg in node_ws:
                        await ws.send_text(msg)
                except Exception:
                    pass

            await asyncio.gather(client_to_node(), node_to_client())
    except Exception as e:
        try:
            await ws.send_text(f"\r\n[tui] connection failed: {e}\r\n")
            await ws.close()
        except Exception:
            pass


# --- Pages (HTML) ---

@app.get("/tui")
async def tui_page():
    resp = await _serve_platform_file("split.html")
    return resp or PlainTextResponse("split.html not found", status_code=500)


# jerpint: this one will be important to nail we might review onboarding flow
@app.get("/onboard")
async def onboard_page():
    resp = await _serve_platform_file("onboard.html")
    return resp or PlainTextResponse("onboard.html not found", status_code=500)


# --- Catch-all: static files ---

@app.get("/{path:path}")
async def catch_all(path: str, request: Request):
    # Root → home.html or site index
    if path == "" or path == "/":
        resp = await _serve_platform_file("home.html")
        if resp:
            return resp
        return await _serve_static("/index.html", request) or PlainTextResponse("Not found", status_code=404)

    # Try wolt site first, then platform public dir
    resp = await _serve_static(f"/{path}", request)
    if resp:
        return resp
    resp = await _serve_platform_file(path)
    if resp:
        return resp
    return PlainTextResponse("Not found", status_code=404)


_start_time = time.time()
