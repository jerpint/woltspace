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

To read or change an app's data through its API, call the app **on its own port**:

```bash
woltspace app list                       # notes  on  127.0.0.1:4321
curl http://127.0.0.1:4321/api/entries   # the app's own API
```

- **Never go through the lodge's port** (`http://my-app.localhost:7777/`, `/app/my-app/`). Those addresses are for the owner's browser and the viewport, and they are moving behind the Mill, which lets only the owner through.
- **If the app has its own login**, send the app's own token. Never send the lodge key to an app.
- **Editing the app's files or database directly** is fine too. Prefer the app's API when it has one, so the app's own checks run.

## Pushing to the viewport

```bash
push-view http://my-app.localhost:7777/
```

Use the **subdomain pattern** `http://<app-name>.localhost:7777/` to push an app to the viewport.

> **Why not `/app/my-app/`?** The `/app/` path triggers a 302 redirect to the app's bare port (e.g. `localhost:4010`). This works locally but breaks through the Cloudflare tunnel, since the redirect target isn't reachable from the outside. The subdomain pattern avoids the redirect entirely.

## Ports

Each app declares its own port in `woltspace.json` (required). Use the **4000-5999** range for apps. The port is permanent — it never changes between restarts. Pick one that doesn't conflict with other apps. If two apps claim the same port, the second one to start gets an error — just pick a different port.

Wolt sites auto-allocate in the **6000+** range, so no collisions. The platform also sets the `PORT` env var to match your manifest port when starting. Avoid 7777 (platform server) and 3001 (TUI).

## Sharing (public access)

Apps are private by default — only accessible locally. Sharing is the owner's call: with lodge auth on, these routes need the owner key, so ask the human. Set `"public": true` in `woltspace.json` to share, or use the API:

```bash
# Share a running app
curl -X POST http://localhost:7777/apps/my-app/share

# Unshare
curl -X POST http://localhost:7777/apps/my-app/unshare

# Panic button — unshare ALL apps
curl -X POST http://localhost:7777/apps/unshare-all
```

### How sharing works

There are two modes, selected automatically:

**Subdomain routing (named tunnel):** If the lodge has a named tunnel (`CLOUDFLARE_TUNNEL_URL` is set), public apps are served at `{app-name}.{domain}` — e.g. `corework.woltspace.com`. No per-app tunnel is spawned. The server's subdomain proxy middleware routes requests to the app's port. URLs are stable, auth-protected by Cloudflare Access, and work automatically for any app.

**Quick tunnels (fallback):** If there's no named tunnel, a per-app `cloudflared` tunnel starts with a random `trycloudflare.com` URL. Uses `--http-host-header localhost` so Vite/Next.js/Astro allowedHosts checks pass. URLs are random and change on restart.

The mode is automatic — wolts don't need to know or care which is active.

### Setup for subdomain routing

Requires a one-time Cloudflare setup after the named tunnel is configured: wildcard DNS, tunnel ingress rule, and Access policy. See `docs/wildcard-subdomain-setup.md` for step-by-step instructions, or load the woltspace cloudflare skill, which covers this.

**Kill switch:** Set `WOLTSPACE_SHARING_ENABLED=0` to disable all sharing. The API will reject share requests and `public: true` is ignored.

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
- **Never edit `$WOLTSPACE_DIR/`** — that's the platform install
- **Apps are portable** — should work if copied out of woltspace
- **Write woltspace.json after setup** — or the app is invisible
- **Use `$PORT` in start commands** — the platform expands it to your manifest port
- **Add `--host 0.0.0.0`** — required for the dev server to accept tunnel traffic
