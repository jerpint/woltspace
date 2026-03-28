---
name: projects
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
  "start": "npm run dev",
  "keeper": "your-wolt-name"
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
| `start` | no | Start command. Null = can't start from lodge. |
| `source` | no | Origin URL if cloned |
| `emoji` | no | Display emoji (auto-assigned) |

**Important:** Only `woltspace.json` is recognized. Not `project.json`, not `app.json`.

## Serving a project

Three modes, auto-detected:

### 1. Static files (simplest)

Put HTML/CSS/JS in the project directory. Served at `/project/{name}/`. No config needed.

### 2. Built static (Vite, Astro, etc.)

Build to `dist/` and set the framework base path to `/project/{name}/`:
- Vite: `base: '/project/my-app/'` in `vite.config.js`
- Astro: `base: '/project/my-app/'` in `astro.config.mjs`

### 3. Dev server (any language/framework)

The platform starts the server and sets the `PORT` env var. The proxy strips the `/project/{name}` prefix — your server sees clean paths.

## Starting and stopping

From the lodge, users click start/stop on project cards. From code:

```bash
# List projects
curl http://localhost:7777/projects

# Start (platform allocates port and runs start command)
curl -X POST http://localhost:7777/projects/my-app/start

# Stop
curl -X POST http://localhost:7777/projects/my-app/stop
```

## Pushing to the viewport

```bash
push-view /project/my-app/
```

## Ports

Each project declares its own port in `woltspace.json` (required). Use the **4000-5999** range for projects. The port is permanent — it never changes between restarts. Pick one that doesn't conflict with other projects. If two projects claim the same port, the second one to start gets an error — just pick a different port.

Wolt sites auto-allocate in the **6000+** range, so no collisions. The platform also sets the `PORT` env var to match your manifest port when starting. Avoid 7777 (platform server) and 3001 (TUI).

## Key rules

- **Never edit `/workspace/woltspace/`** — that's the platform
- **Projects are portable** — should work if copied out of woltspace
- **Write woltspace.json after setup** — or the project is invisible
- **One dev server at a time** is fine; max 2 concurrent; be mindful of resources
