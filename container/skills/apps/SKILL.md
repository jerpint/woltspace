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
mkdir -p "$WOLTSPACE_WOLTS_DIR/apps/my-app"
cd "$WOLTSPACE_WOLTS_DIR/apps/my-app"
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
| `port` | yes | Fixed app port in the 4000-5999 range. Permanent — survives restarts. Do not use platform service ports. |
| `start` | no | Start command. Use `$PORT` — the platform expands it. Null = can't start from lodge or be served as an app. |
| `source` | no | Origin URL if cloned |
| `emoji` | no | Display emoji (auto-assigned) |
| `public` | no | Ignored (kept so old manifests load). Share through the share list or an opt-in quick tunnel. |

**Important:** Only `woltspace.json` is recognized. Not `project.json`, not `app.json`.

## Serving an app

### Running app server

The platform starts the server and sets the `PORT` env var. The app gateway forwards HTTP, WebSockets, and SSE to that server.

### Static HTML apps

There is no static-file fallback in the lodge or gateway. A static HTML app needs a start command just like every other app, for example:

```json
"start": "python3 -m http.server $PORT --bind 127.0.0.1"
```

When the process is stopped, the gateway shows the normal stopped-app page.

## Starting and stopping

**ALWAYS use the platform API. Never run the start command directly.**

Running `npm run dev`, `python server.py`, or any start command directly bypasses the platform. The app will show as "off" in the viewport even if the server is running.

```bash
# Start an app
curl -X POST "$WOLTSPACE_API/apps/my-app/start"

# Stop an app
curl -X POST "$WOLTSPACE_API/apps/my-app/stop"

# List all apps + running state
curl "$WOLTSPACE_API/apps"
```

## Pushing to the viewport

```bash
push-view http://my-app.localhost:7117/
```

Use the app gateway address shown by `GET /apps`. The first lodge defaults to `http://<app-name>.localhost:7117/`; `/app/<app-name>/` redirects there for compatibility.

## Ports

Each app declares its own port in `woltspace.json` (required). Use the **4000-5999** range for apps. The port is permanent — it never changes between restarts. Pick one that doesn't conflict with other apps. If two apps claim the same port, the second one to start gets an error — just pick a different port.

The control plane address is `$WOLTSPACE_API`; never assume its port. The app gateway follows the lodge port minus 660 unless Settings overrides it. Apps must stay in the **4000-5999** range and must not use their lodge's gateway port. The platform sets `PORT` to the app manifest's value when it starts the app. Wolt sites do not allocate their own ports; the lodge serves them at `/wolt/<name>/site/`.

## Sharing

Apps are private by default: only the lodge owner, locally, through the app gateway.

**Share with people (the normal way):** the lodge owner adds exact emails or `@domain` entries to the app's share list, from the app page or with `woltspace app share <app> friend@example.com` (`woltspace app sharing <app>` lists it, `unshare` removes). Visitors open `https://<app>.<apps domain>`, sign in through Cloudflare Access, and the gateway checks the list. This needs an app domain; see the woltspace cloudflare skill.

**Quick tunnels (opt-in, apps only):** a random `trycloudflare.com` link that gives anyone who has it the app, with no login. Off unless the lodge owner sets `WOLTSPACE_APP_QUICK_TUNNELS=1` in the lodge's `.env`. Then:

```bash
curl -X POST "$WOLTSPACE_API/apps/my-app/share"     # open one
curl -X POST "$WOLTSPACE_API/apps/my-app/unshare"   # close it
curl -X POST "$WOLTSPACE_API/apps/unshare-all"      # close them all
```

A quick tunnel is never opened for the lodge or the gateway port, and the manifest's `public` field is ignored: a manifest can never publish an app.

## Key rules

- **Always use the API to start/stop** — never run start commands directly
- **Never edit `$WOLTSPACE_DIR/`** — that's the platform install
- **Apps are portable** — should work if copied out of woltspace
- **Write woltspace.json after setup** — or the app is invisible
- **Use `$PORT` in start commands** — the platform expands it to your manifest port
- **Add `--host 0.0.0.0`** — required for the dev server to accept tunnel traffic
