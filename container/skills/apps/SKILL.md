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
| `public` | no | Ignored since 0.6: sharing is `woltspace app share`, never a manifest field. Leave it out. |

**Important:** Only `woltspace.json` is recognized. Not `project.json`, not `app.json`.

## Serving an app

### Running dev server (primary mode)

The platform starts the server with `PORT` and `HOST=127.0.0.1`. The Mill proxies `http://{name}.localhost:7777/` to it, streamed both ways: internal links, WebSockets and SSE work as on the app's own port.

### Static files (fallback)

An app with no `start` command is served by the Mill from its `dist/` folder, or its folder if there's none, at `http://{name}.localhost:7777/`. Dotfiles (`.env`, `.git`) are never served.

## Starting and stopping

**ALWAYS use the `woltspace app` CLI. Never run the start command directly.**

Running `npm run dev`, `python server.py`, or any start command directly bypasses the platform. The app will show as "off" in the viewport even if the server is running.

```bash
woltspace app start my-app
woltspace app stop my-app
woltspace app restart my-app
woltspace app logs my-app --tail 50
woltspace app list          # every app, on or off, and its own port
```

The CLI sends your lodge key. A raw `curl` to `localhost:7777` doesn't, so with lodge auth on it gets `401`.

## Using an app from a wolt

From a wolt session, read or change an app's data through its API by calling the app **on its own port**:

```bash
woltspace app list                       # notes  on  127.0.0.1:4321
curl http://127.0.0.1:4321/api/entries   # the app's own API
```

- **`127.0.0.1` is the lodge's own machine.** In a container lodge that means inside the container, where wolts and apps run together; the host doesn't need those ports.
- **An app that's `off`** has no listener: start it first (`woltspace app start`), then use the listed port.
- **A `static` app has no server and no API.** Read its files; to preview it, push its browser address to the viewport.
- **Never go through the lodge's port** (`http://my-app.localhost:7777/`, `/app/my-app/`). Those addresses are for the owner's browser and the viewport: they go through the Mill, which lets only the owner through.
- **If the app has its own login**, send the app's own token. Never send the lodge key to an app.
- **Prefer the app's API to editing its data directly**, so the app's own checks run. Change a running app's database by hand only when the human asked, and respect its locking and schema (stop the app first if unsure).

## Pushing to the viewport

```bash
push-view http://my-app.localhost:7777/
```

Use the **subdomain pattern** `http://<app-name>.localhost:7777/` to push an app to the viewport. That URL is for the owner's browser; your own calls to the app go to its port (see above).

> **Why not `/app/my-app/`?** `/app/<name>/` only redirects to the app's own address now. The subdomain is the app's real home: the Mill serves it there, on an origin of its own.

## Ports

Each app declares its own port in `woltspace.json` (required). Use the **4000-5999** range for apps. The port is permanent — it never changes between restarts. Pick one that doesn't conflict with other apps. If two apps claim the same port, the second one to start gets an error — just pick a different port.

The platform sets `PORT` to your manifest port and `HOST=127.0.0.1` when starting: pass `--host $HOST`, not `0.0.0.0`, so the app is reachable from this machine only, not the network (`woltspace doctor` names apps that bind `0.0.0.0`). Names starting with `site-` are reserved for wolt sites and aren't served. Avoid 7777 (platform server) and 3001 (TUI).

## Reaching an app from elsewhere

Apps are private: locally, the owner reaches `http://<app-name>.localhost:7777/`; through the lodge's tunnel, `https://<app-name>.<domain>/`. Both go through the Mill (see `docs/mill.md`). Through the tunnel, the lodge lets a request in only with the owner's Cloudflare Access token for the apps' Access application. That needs `access` settings in `woltspace.json`, which only the human sets (`container/migrations/v0.6.0.md`).

## Sharing an app with anyone

Sharing lets **anyone with the link** use one app. It's the human's choice. You may carry it out **only when the human asks** for that app.

```bash
woltspace app share my-app       # prints the public link and the warning
woltspace app unshare my-app     # private again
woltspace app unshare-all        # every app private again
```

- **Before sharing, tell the human plainly:** "Anyone with this link can use my-app and do whatever it can do: read and change its data, use its API keys." Share only after they confirm. An app with its own login keeps that login.
- **The link:** a quick tunnel (`https://<random>.trycloudflare.com`) needs no domain. It changes when the lodge restarts, so give the human the new one from `woltspace app list`. With the lodge's own domain, `https://my-app.<domain>/` works too, once the human's Cloudflare Access policy lets visitors through for that host.
- **What stays protected:** a visitor reaches only that app, through the Mill. They never get the human's identity or credentials, the lodge, wolt sites or other apps.
- **Every share is logged** (`.space/logs/sharing.log`) and listed by `woltspace app list` and `woltspace doctor`. Unshare when the human is done.

### The lodge is never shared

The lodge (port 7777, its tunnel address, its terminals and API) is the human's control room. **Never expose it:** no tunnel, port-forward, proxy or `0.0.0.0` bind that reaches it, and no sharing of a wolt's site. If the human asks for that, warn them that anyone who reaches the lodge can act as them. Suggest sharing the one app they need instead. Refuse unless they insist explicitly, knowing that risk.

## Secrets

An app keeps its own secrets in its own folder, never the lodge's.

- **Use the app's own `.env`** (`/workspace/wolts/apps/<name>/.env`), gitignored, loaded by the app itself (python-dotenv, Vite's `.env`, etc.).
- **Don't reuse the lodge's tokens.** Apps no longer receive the lodge's chat, GitHub-bot or Cloudflare credentials (`TELEGRAM_*`, `SLACK_*`, `GITHUB_APP_*`, `CLOUDFLARE_*`). If an app needs Slack or Telegram, ask the human for a separate bot or token just for that app, so a leak exposes one app, not the lodge.
- **Never print a secret, log it, or commit it.** Your transcript, the app log and git history all outlive the session.
- **Never put a secret on a command line** (`cmd --token abc`, `KEY=abc` inside a start command). Every user on the machine can read command lines with `ps`. Pass secrets in the environment or a file instead.
- **Ask, don't hunt.** If a key is missing, ask the human for it. Don't read other wolts' or the lodge's `.env` to find one.

## Key rules

- **Always use `woltspace app start/stop`** — never run start commands directly
- **Call apps on their own port** (`127.0.0.1:<port>`), never through 7777
- **Share an app only when the human asks**, with the warning above. Never expose the lodge
- **Never edit `$WOLTSPACE_DIR/`** — that's the platform install
- **Apps are portable** — should work if copied out of woltspace
- **Write woltspace.json after setup** — or the app is invisible
- **Use `$PORT` in start commands** — the platform expands it to your manifest port
- **Use `--host $HOST`, never `0.0.0.0`**: the Mill reaches the app on loopback, tunnel traffic included
