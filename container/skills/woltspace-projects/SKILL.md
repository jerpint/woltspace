---
name: woltspace-projects
description: Work with projects — the isolated workspace for building apps, scripts, and experiments. Use when creating, running, or managing a project.
---

# Projects — Isolated Workspaces

Projects are things you ship — apps, tools, services. They live at `wolts/projects/{name}/` and are served at `/project/{name}/`. Each project is self-contained with its own code, deps, and server.

**Projects are an escalation from sites.** Your site (`wolt/site/`) is your lightweight private workspace. A project is for when the work needs its own server, dependencies, or is meant to be shared. Always confirm with the user before creating one.

## Sites vs Projects

| | Site (`wolt/site/`) | Project (`wolts/projects/`) |
|---|---|---|
| **Purpose** | Your private workspace | Something you ship |
| **Visibility** | Private to the wolt owner | Can be public |
| **Complexity** | Static HTML/CSS/JS, lightweight | Own server, deps, manifest |
| **Created by** | Wolts, freely | User opts in |
| **Manifest** | None needed | `woltspace.json` required |
| **Example** | Personal digest, scratch mockups | Workout tracker, shared tool |

**When to suggest a project:** the user wants a backend, deps, sharing, or something that outgrows static HTML. Say: "This is getting complex — want me to set it up as a project?"

## Creating a project

```bash
mkdir -p /workspace/wolts/projects/my-app
cd /workspace/wolts/projects/my-app
# ... set up your code
```

Then write `woltspace.json` — **this is required** for the platform to discover and serve the project:

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
| `description` | no | What the project does |
| `stack` | no | `python`, `vite`, `node`, or `html` |
| `install` | no | Install command |
| `port` | yes | Fixed port for this project. Permanent — survives restarts. Avoid 7777 and 3001. |
| `start` | no | Start command. Use `$PORT` — the platform expands it. Add `--host 0.0.0.0` for network access. Null = can't start from lodge. |
| `source` | no | Origin URL if cloned |
| `emoji` | no | Display emoji (auto-assigned) |
| `public` | no | If `true`, a cloudflared tunnel starts automatically with the project. Default: `false`. |

**Important:** Only `woltspace.json` is recognized. Not `project.json`, not `app.json`.

## Serving a project

### Running dev server (primary mode)

The platform starts the server and sets the `PORT` env var. The viewport iframe loads the project's port **directly** — no proxy. This means internal links, WebSockets, and SSE all work naturally.

### Static files (fallback)

If the project isn't running, static files in `dist/` or the project root are served at `/project/{name}/`.

## Starting and stopping

**ALWAYS use the platform API. Never run the start command directly.**

Running `npm run dev`, `python server.py`, or any start command directly bypasses the platform. The project will show as "off" in the viewport even if the server is running.

```bash
# Start a project
curl -X POST http://localhost:7777/projects/my-app/start

# Stop a project
curl -X POST http://localhost:7777/projects/my-app/stop

# List all projects + running state
curl http://localhost:7777/projects
```

## Pushing to the viewport

```bash
push-view /project/my-app/
```

The viewport will load the project at its direct port (e.g. `localhost:4010`). No proxy involved.

## Ports

Each project declares its own port in `woltspace.json` (required). Use the **4000-5999** range for projects. The port is permanent — it never changes between restarts. Pick one that doesn't conflict with other projects. If two projects claim the same port, the second one to start gets an error — just pick a different port.

Wolt sites auto-allocate in the **6000+** range, so no collisions. The platform also sets the `PORT` env var to match your manifest port when starting. Avoid 7777 (platform server) and 3001 (TUI).

## Sharing (public access)

Projects are private by default — only accessible locally. To share:

```bash
# Share a running project (starts a cloudflared tunnel)
curl -X POST http://localhost:7777/projects/my-app/share

# Unshare (kills the tunnel)
curl -X POST http://localhost:7777/projects/my-app/unshare

# Panic button — unshare ALL projects
curl -X POST http://localhost:7777/projects/unshare-all
```

Or set `"public": true` in `woltspace.json` — the tunnel starts automatically when the project starts.

**How it works:** `cloudflared` creates a tunnel to the project's port with `--http-host-header localhost` (rewrites the Host header so Vite/Next.js/Astro allowedHosts checks pass — no project config changes needed). The tunnel URL is stored in the project's running state.

**Kill switch:** Set `WOLTSPACE_SHARING_ENABLED=0` to disable all sharing. The API will reject share requests and `public: true` is ignored.

## Key rules

- **Always use the API to start/stop** — never run start commands directly
- **Never edit `/workspace/woltspace/`** — that's the platform
- **Projects are portable** — should work if copied out of woltspace
- **Write woltspace.json after setup** — or the project is invisible
- **Use `$PORT` in start commands** — the platform expands it to your manifest port
- **Add `--host 0.0.0.0`** — required for the dev server to accept tunnel traffic
