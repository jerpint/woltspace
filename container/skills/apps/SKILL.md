---
name: apps
description: Work with apps — the isolated workspace for building apps, scripts, and experiments. Use when creating, running, or managing an app.
---

# Apps — Isolated Workspaces

Apps are things you ship — apps, tools, services. They live at `wolts/apps/{name}/` and are served by the Mill at `http://{name}.localhost:7777/`, each on an origin of its own. Each app is self-contained with its own code, deps, and server.

**Apps are an escalation from sites.** Your site (`wolt/site/`) is your lightweight private workspace. An app is for when the work needs its own server or dependencies. Always confirm with the user before creating one.

## Sites vs Apps

| | Site (`wolt/site/`) | App (`wolts/apps/`) |
|---|---|---|
| **Purpose** | Your private workspace | Something you ship |
| **Visibility** | The owner only | The owner only, locally and through the lodge's tunnel |
| **Complexity** | Static HTML/CSS/JS, lightweight | Own server, deps, manifest |
| **Created by** | Wolts, freely | User opts in |
| **Manifest** | None needed | `woltspace.json` required |
| **Example** | Personal digest, scratch mockups | Workout tracker, a tool with its own server |

**When to suggest an app:** the user wants a backend, deps, a build step, or something that outgrows static HTML. Say: "This is getting complex — want me to set it up as an app?"

## Creating an app

```bash
mkdir -p /workspace/wolts/apps/my-app
cd /workspace/wolts/apps/my-app
# ... set up your code
```

Then write `woltspace.json` — **this is required** for the platform to discover and serve the app:

```json
{
  "name": "my-app",
  "description": "What this does",
  "stack": "node",
  "port": 4010,
  "install": "npm install",
  "start": "npm run dev --port $PORT --host $HOST",
  "keeper": "your-wolt-name"
}
```

### Fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | yes | Matches directory name, globally unique |
| `keeper` | yes | Your wolt name — who owns this |
| `description` | no | What the app does |
| `stack` | no | `python`, `vite`, `node`, or `html` |
| `install` | no | Install command |
| `port` | yes | Fixed port for this app. Permanent — survives restarts. Avoid 7777 and 3001. |
| `start` | no | Start command. Use `$PORT` and `--host $HOST` (the platform sets `HOST=127.0.0.1`: the app is reachable from this machine only, and the browser goes through the Mill). Never `0.0.0.0`. Null = a static app, served by the Mill from `dist/` or the app folder. |
| `source` | no | Origin URL if cloned |
| `emoji` | no | Display emoji (auto-assigned) |
| `public` | no | Ignored since 0.6: there is no per-app public sharing. Leave it out. |

**Important:** Only `woltspace.json` is recognized. Not `project.json`, not `app.json`.

## Serving an app

### Running dev server (primary mode)

The platform starts the server with `PORT` and `HOST=127.0.0.1`. The Mill proxies `http://{name}.localhost:7777/` to it, streamed both ways: internal links, WebSockets and SSE work as on the app's own port.

### Static files (fallback)

An app with no `start` command is served by the Mill from its `dist/` folder, or its folder if there's none, at `http://{name}.localhost:7777/`. Dotfiles (`.env`, `.git`) are never served.

## Starting and stopping

**ALWAYS use the platform API. Never run the start command directly.**

Running `npm run dev`, `python server.py`, or any start command directly bypasses the platform. The app will show as "off" in the viewport even if the server is running.

```bash
# Start an app
curl -X POST http://localhost:7777/apps/my-app/start

# Stop an app
curl -X POST http://localhost:7777/apps/my-app/stop

# List all apps + running state
curl http://localhost:7777/apps
```

## Pushing to the viewport

```bash
push-view http://my-app.localhost:7777/
```

Use the **subdomain pattern** `http://<app-name>.localhost:7777/` to push an app to the viewport.

> **Why not `/app/my-app/`?** `/app/<name>/` only redirects to the app's own address now. The subdomain is the app's real home: the Mill serves it there, on an origin of its own.

## Ports

Each app declares its own port in `woltspace.json` (required). Use the **4000-5999** range for apps. The port is permanent — it never changes between restarts. Pick one that doesn't conflict with other apps. If two apps claim the same port, the second one to start gets an error — just pick a different port.

The platform sets `PORT` to your manifest port and `HOST=127.0.0.1` when starting: pass `--host $HOST`, not `0.0.0.0`, so the app is reachable from this machine only, not the network (`woltspace doctor` names apps that bind `0.0.0.0`). Names starting with `site-` are reserved for wolt sites and aren't served. Avoid 7777 (platform server) and 3001 (TUI).

## Reaching an app from elsewhere

Apps are private: locally, the owner reaches `http://<app-name>.localhost:7777/`; through the lodge's tunnel, `https://<app-name>.<domain>/`. Both go through the Mill (see `docs/mill.md`). Through the tunnel, the lodge lets a request in only with the owner's Cloudflare Access token for the apps' Access application. That needs `access` settings in `woltspace.json`, which only the human sets (`container/migrations/v0.6.0.md`).

There is no sharing an app with other people in 0.6: the share routes, `"public": true` and quick tunnels are gone, and the lodge lets only the owner's email in. Never tell anyone an app is shared.

## Secrets

An app keeps its own secrets in its own folder, never the lodge's.

- **Use the app's own `.env`** (`/workspace/wolts/apps/<name>/.env`), gitignored, loaded by the app itself (python-dotenv, Vite's `.env`, etc.).
- **Don't reuse the lodge's tokens.** Apps no longer receive the lodge's chat, GitHub-bot or Cloudflare credentials (`TELEGRAM_*`, `SLACK_*`, `GITHUB_APP_*`, `CLOUDFLARE_*`). If an app needs Slack or Telegram, ask the human for a separate bot or token just for that app, so a leak exposes one app, not the lodge.
- **Never print a secret, log it, or commit it.** Your transcript, the app log and git history all outlive the session.
- **Never put a secret on a command line** (`cmd --token abc`, `KEY=abc` inside a start command). Every user on the machine can read command lines with `ps`. Pass secrets in the environment or a file instead.
- **Ask, don't hunt.** If a key is missing, ask the human for it. Don't read other wolts' or the lodge's `.env` to find one.

## Key rules

- **Always use the API to start/stop** — never run start commands directly
- **Never edit `$WOLTSPACE_DIR/`** — that's the platform install
- **Apps are portable** — should work if copied out of woltspace
- **Write woltspace.json after setup** — or the app is invisible
- **Use `$PORT` in start commands** — the platform expands it to your manifest port
- **Use `--host $HOST`, never `0.0.0.0`**: the Mill reaches the app on loopback, tunnel traffic included
