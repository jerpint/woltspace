---
name: woltspace-apps
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

> **Why not `/app/my-app/`?** The `/app/` path triggers a 302 redirect to the app's bare port (e.g. `localhost:4010`). This works locally but breaks through the Cloudflare tunnel, since the redirect target isn't reachable from the outside. The subdomain pattern avoids the redirect entirely.

## Ports

Each app declares its own port in `woltspace.json` (required). Use the **4000-5999** range for apps. The port is permanent — it never changes between restarts. Pick one that doesn't conflict with other apps. If two apps claim the same port, the second one to start gets an error — just pick a different port.

Wolt sites auto-allocate in the **6000+** range, so no collisions. The platform also sets the `PORT` env var to match your manifest port when starting. Avoid 7777 (platform server) and 3001 (TUI).

## Sharing (public access)

Apps are private by default — only accessible locally. Set `"public": true` in `woltspace.json` to share, or use the API:

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

Requires a one-time Cloudflare setup after the named tunnel is configured: wildcard DNS, tunnel ingress rule, and Access policy. See `docs/wildcard-subdomain-setup.md` for step-by-step instructions, or use the `/woltspace-setup-tunnel` skill which covers this.

**Kill switch:** Set `WOLTSPACE_SHARING_ENABLED=0` to disable all sharing. The API will reject share requests and `public: true` is ignored.

## Key rules

- **Always use the API to start/stop** — never run start commands directly
- **Never edit `/workspace/woltspace/`** — that's the platform
- **Apps are portable** — should work if copied out of woltspace
- **Write woltspace.json after setup** — or the app is invisible
- **Use `$PORT` in start commands** — the platform expands it to your manifest port
- **Add `--host 0.0.0.0`** — required for the dev server to accept tunnel traffic
