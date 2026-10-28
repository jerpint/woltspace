---
name: apps
description: Work with apps — the isolated workspace for building apps, scripts, and experiments. Use when creating, running, or managing an app.
---

# Apps — Isolated Workspaces

Apps are things you ship — apps, tools, services. They live at `wolts/apps/{name}/` and are served at `/app/{name}/`. Each app is self-contained with its own code, deps, and server.

**Apps are an escalation from sites.** Your site (`wolt/site/`) is your lightweight private workspace. An app is for when the work needs its own server, dependencies, or is meant to be shared. Always confirm with the user before creating one.

## Sites vs Apps

| | Site (`wolt/site/`) | App (`wolts/apps/`) |
|---|---|---|
| **Purpose** | Your private workspace | Something you ship |
| **Visibility** | Private to the wolt owner | Can be public |
| **Complexity** | Static HTML/CSS/JS, lightweight | Own server, deps, manifest |
| **Created by** | Wolts, freely | User opts in |
| **Manifest** | None needed | `woltspace.json` required |
| **Example** | Personal digest, scratch mockups | Workout tracker, shared tool |

**When to suggest an app:** the user wants a backend, deps, sharing, or something that outgrows static HTML. Say: "This is getting complex — want me to set it up as an app?"

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
  "start": "npm run dev --port $PORT --host 0.0.0.0",
  "keeper": "your-wolt-name",
  "public": false
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
| `start` | no | Start command. Use `$PORT` — the platform expands it. Add `--host 0.0.0.0` for network access. Null = can't start from lodge. |
| `source` | no | Origin URL if cloned |
| `emoji` | no | Display emoji (auto-assigned) |
| `public` | no | If `true`, the app is shared publicly when started. With a named tunnel: served at `{name}.{domain}` (e.g. `corework.woltspace.com`). Without: a random quick tunnel URL. Default: `false`. |

**Important:** Only `woltspace.json` is recognized. Not `project.json`, not `app.json`.

## Serving an app

### Running dev server (primary mode)

The platform starts the server and sets the `PORT` env var. The viewport iframe loads the app's port **directly** — no proxy. This means internal links, WebSockets, and SSE all work naturally.

### Static files (fallback)

If the app isn't running, static files in `dist/` or the app root are served at `/app/{name}/`.

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

The platform sets `PORT` to your manifest port and `HOST=127.0.0.1` when starting: pass `--host $HOST`, not `0.0.0.0`, so the app is reached only through the Mill (`woltspace doctor` names apps that bind `0.0.0.0`). Names starting with `site-` are reserved for wolt sites and aren't served. Avoid 7777 (platform server) and 3001 (TUI).

## Reaching an app from elsewhere

Apps are private: locally, the owner reaches `http://<app-name>.localhost:7777/`; through the lodge's tunnel, `https://<app-name>.<domain>/`. Both go through the Mill (see `docs/mill.md`). Through the tunnel, the lodge lets a request in only with the owner's Cloudflare Access token for the apps' Access application. That needs `access` settings in `woltspace.json`, which only the human sets (`container/migrations/v0.6.0.md`).

There is no per-app public sharing any more: the share routes, `"public": true` and quick tunnels are gone. If your human wants someone else to see an app, that's a change to who the Access application lets in, made by the human.

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
- **Add `--host 0.0.0.0`** — required for the dev server to accept tunnel traffic
