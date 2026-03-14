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
    APPS_DIR,
    APP_MIME_TYPES,
    DEN_REPLY_FOOTER,
    MIME_TYPES,
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


# --- Digest cron ---

def _start_digest_cron():
    """Digest cron — runs at 6am and 3pm Montreal time."""
    import threading
    from datetime import datetime
    from zoneinfo import ZoneInfo

    digest_enabled = (os.environ.get("ENABLE_DIGEST_CRON") or load_dotenv().get("ENABLE_DIGEST_CRON", "")).lower() == "true"
    if not digest_enabled:
        return

    digest_script = WOLT_DIR.parent / "woltspace" / "cron" / "digest.mjs"
    digest_flag = STATE_DIR / "digest-last-run.txt"
    digest_3pm_flag = STATE_DIR / "digest-3pm-run.txt"
    tz = ZoneInfo("America/Montreal")

    def _spawn_digest(reason: str):
        if not digest_script.exists():
            print(f"[cron] digest script not found at {digest_script}")
            return
        print(f"[cron] running digest ({reason})")
        dotenv = load_dotenv()
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
        clean_env.update({
            "CLAUDE_CODE_OAUTH_TOKEN": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or dotenv.get("CLAUDE_CODE_OAUTH_TOKEN", ""),
            "SPOTIFY_ID": os.environ.get("SPOTIFY_ID") or dotenv.get("SPOTIFY_ID", ""),
            "SPOTIFY_SECRET": os.environ.get("SPOTIFY_SECRET") or dotenv.get("SPOTIFY_SECRET", ""),
            "SPOTIFY_ACCESS_TOKEN": os.environ.get("SPOTIFY_ACCESS_TOKEN") or dotenv.get("SPOTIFY_ACCESS_TOKEN", ""),
            "SPOTIFY_REFRESH_TOKEN": os.environ.get("SPOTIFY_REFRESH_TOKEN") or dotenv.get("SPOTIFY_REFRESH_TOKEN", ""),
            "SPOTIFY_USER": os.environ.get("SPOTIFY_USER") or dotenv.get("SPOTIFY_USER", ""),
            "WOLT_NAME": WOLT_NAME,
            "WOLT_DIR": str(WOLT_DIR),
            "NODE_PATH": "/workspace/woltspace/node_modules",
        })
        child = subprocess.Popen(
            ["node", str(digest_script)],
            env=clean_env,
            start_new_session=True,
        )
        write_status({"digest": {"state": "running", "startedAt": int(time.time() * 1000), "reason": reason, "pid": child.pid}})

    def _cron_loop():
        while True:
            time.sleep(60)
            now = datetime.now(tz)
            today = now.strftime("%Y-%m-%d")
            h = now.hour

            # 6am run
            last_run = digest_flag.read_text().strip() if digest_flag.exists() else ""
            if h >= 6 and last_run != today:
                digest_flag.write_text(today)
                _spawn_digest("6am daily")

            # 3pm run
            last_3pm = digest_3pm_flag.read_text().strip() if digest_3pm_flag.exists() else ""
            if h >= 15 and last_3pm != today:
                digest_3pm_flag.write_text(today)
                _spawn_digest("3pm afternoon")

    t = threading.Thread(target=_cron_loop, daemon=True)
    t.start()
    print("[cron] digest cron enabled")


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
    _start_digest_cron()
    print(f"""
  woltspace server (python) · http://localhost:{3000}
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
        set_current_url(url, session, body.get("port", 3000))
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
        "has_oauth": Path("/home/node/.claude/.credentials.json").exists() or bool(env.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")),
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

@app.post("/sessions/new")
async def session_new(request: Request):
    body = await request.json()
    prompt = body.get("prompt")
    if not prompt:
        return JSONResponse({"error": "prompt required"}, status_code=400)
    session_name = f"{WOLT_NAME}-{int(time.time() * 1000) % 100000}"
    reg_data = {
        "name": session_name,
        "wolt": WOLT_NAME,
        "status": "running",
        "created_at": int(time.time()),
        "dir": str(WOLT_DIR),
        "prompt": prompt[:500],
        "last_activity": int(time.time()),
    }
    try:
        SESSION_REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
        (SESSION_REGISTRY_DIR / f"{session_name}.json").write_text(json.dumps(reg_data, indent=2) + "\n")
    except Exception as e:
        print(f"[sessions] registry write failed: {e}")
    run_script = Path(__file__).resolve().parent.parent / "container" / "bin" / "run-session.sh"
    cmd = f"{run_script} {_shell_quote(session_name)} {_shell_quote(str(WOLT_DIR))} {_shell_quote(prompt)}"
    try:
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", session_name, "-c", str(WOLT_DIR), cmd],
            check=True,
        )
        print(f"[sessions] spawned {session_name}")
        return {"name": session_name}
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


# --- Apps ---

@app.get("/apps")
async def list_apps():
    apps = []
    if APPS_DIR.exists():
        for entry in APPS_DIR.iterdir():
            app_json = entry / "app.json"
            if not app_json.exists():
                continue
            try:
                config = json.loads(app_json.read_text())
                has_dist = (entry / "dist").exists()
                apps.append({
                    "name": entry.name,
                    "url": f"/app/{entry.name}/",
                    "mode": "static" if has_dist else ("proxy" if config.get("port") else "unconfigured"),
                    **config,
                })
            except Exception:
                pass
    return apps


@app.get("/app/{app_name}/{path:path}")
@app.get("/app/{app_name}")
async def serve_app(app_name: str, request: Request, path: str = ""):
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9_-]*$", app_name):
        return JSONResponse({"error": "invalid app name"}, status_code=400)
    app_dir = APPS_DIR / app_name
    app_json_path = app_dir / "app.json"
    if not app_json_path.exists():
        return JSONResponse({"error": f'app "{app_name}" not found — missing app.json'}, status_code=404)
    try:
        app_config = json.loads(app_json_path.read_text())
    except Exception:
        return PlainTextResponse("invalid app.json", status_code=500)

    sub_path = "/" + path if path else "/"
    dist_dir = app_dir / "dist"

    # Strategy 1: static
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
        return PlainTextResponse("Not found in app", status_code=404)

    # Strategy 2: proxy
    port = app_config.get("port")
    if not port or port < 1024 or port > 65535:
        return PlainTextResponse(f'app "{app_name}" has no dist/ and no valid port', status_code=500)
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
            return PlainTextResponse(f'App "{app_name}" not running on port {port}', status_code=502)


# --- Projects ---

@app.get("/projects")
async def list_projects():
    """List all projects with their status (has project.json, running port, etc.)."""
    projects = []
    if PROJECTS_DIR.exists():
        for entry in sorted(PROJECTS_DIR.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            proj_json = entry / "project.json"
            config = {}
            if proj_json.exists():
                try:
                    config = json.loads(proj_json.read_text())
                except Exception:
                    pass
            has_dist = (entry / "dist").exists()
            port = config.get("port")
            if has_dist:
                mode = "static"
            elif port:
                mode = "proxy"
            else:
                mode = "directory"
            projects.append({
                "name": entry.name,
                "url": f"/project/{entry.name}/",
                "mode": mode,
                **config,
            })
    return projects


@app.get("/project/{project_name}/{path:path}")
@app.get("/project/{project_name}")
async def serve_project(project_name: str, request: Request, path: str = ""):
    """Serve a project — static files from dist/, or reverse proxy to a running port."""
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9_-]*$", project_name):
        return JSONResponse({"error": "invalid project name"}, status_code=400)
    project_dir = PROJECTS_DIR / project_name
    if not project_dir.exists():
        return JSONResponse({"error": f'project "{project_name}" not found'}, status_code=404)

    # Read project.json if it exists (optional — beaver writes it)
    proj_json_path = project_dir / "project.json"
    proj_config = {}
    if proj_json_path.exists():
        try:
            proj_config = json.loads(proj_json_path.read_text())
        except Exception:
            pass

    sub_path = "/" + path if path else "/"
    dist_dir = project_dir / "dist"

    # Strategy 1: static from dist/
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

    # Strategy 2: proxy to running port
    port = proj_config.get("port")
    if port and 1024 <= port <= 65535:
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
                return PlainTextResponse(f'Project "{project_name}" not running on port {port}', status_code=502)

    # Strategy 3: serve static files directly from project root (simple HTML projects)
    candidates = [project_dir / path, project_dir / path / "index.html"]
    if not path:
        candidates = [project_dir / "index.html"]
    for candidate in candidates:
        resolved = candidate.resolve()
        if not str(resolved).startswith(str(project_dir.resolve())):
            continue
        if resolved.exists() and resolved.is_file():
            ext = resolved.suffix
            mime = MIME_TYPES.get(ext) or APP_MIME_TYPES.get(ext, "application/octet-stream")
            return Response(resolved.read_bytes(), media_type=mime, headers={"Cache-Control": "no-cache"})

    return PlainTextResponse(f'Project "{project_name}" has no servable content', status_code=404)


# --- Shares ---

@app.post("/shares")
async def create_share(request: Request):
    body = await request.json()
    target_session = sanitize_session(body.get("session", "main"))
    session_file = current_url_file(target_session)
    session_data = json.loads(session_file.read_text()) if session_file.exists() else {}
    port = session_data.get("port", 3000)
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
