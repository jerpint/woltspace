---
name: woltspace-new-project
description: Set up a new project from scratch. Use when starting a fresh project — scaffolds the directory, writes woltspace.json, starts the dev server.
---

# New Project Setup

A project is something you ship — an app, a tool, a service. It has its own server, dependencies, and manifest. It can be public. Projects live at `wolts/projects/{name}/` and are served at `/project/{name}/`.

**Projects are an escalation, not a default.** Wolts should start with their site (`wolt/site/`) for lightweight work. Only create a project when the work needs its own server, dependencies, or is meant to be shared. Always confirm with the user before creating a project.

## 1. Confirm with the user

Before creating a project, make sure they want one:

> "This sounds like it needs its own server/deps — want me to set it up as a project? Or keep it simple in your site for now?"

## 2. Create the directory

Projects live in the global projects directory (shared across all wolts):

```bash
mkdir -p /workspace/wolts/projects/{name}
cd /workspace/wolts/projects/{name}
```

## 3. Scaffold it

Based on the user's request, figure out stack and scaffold:

**Simple HTML page:**
```bash
# Just write the files — no deps needed
```

**Vite + React/Vue/Svelte:**
```bash
npm create vite@latest . -- --template react  # or vue, svelte
npm install
```

**Python + FastAPI:**
```bash
uv init . && uv add fastapi uvicorn
```

**Astro blog/site:**
```bash
npm create astro@latest . -- --template blog
```

**Clone a repo:**
```bash
git clone <url> .
# Read the README, figure out deps, install them
```

## 4. Write woltspace.json

This is **required** — the platform only discovers projects that have `woltspace.json`. Without it, the project is invisible.

```json
{
  "name": "{name}",
  "description": "What this project does",
  "stack": "node",
  "port": 4010,
  "install": "npm install",
  "start": "node server.js --port $PORT --host 0.0.0.0",
  "keeper": "{your-wolt-name}",
  "emoji": "🦊",
  "public": false
}
```

### Fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | yes | Project name (matches directory name, globally unique) |
| `keeper` | yes | Your wolt name — who owns this project |
| `description` | no | What the project does |
| `stack` | no | Tech stack: `python`, `vite`, `node`, `html` |
| `install` | no | Install command (e.g. `npm install`, `uv sync`) |
| `port` | yes | Fixed port for this project (use 4000-5999 range). Pick one, it's yours permanently. Avoid 7777 (platform) and 3001 (TUI). Sites use 6000+ so no collisions. |
| `start` | no | Start command. Use `$PORT` — the platform expands it. Add `--host 0.0.0.0` for network access. **Null = project can't be started from the lodge.** |
| `source` | no | Origin URL if cloned/forked |
| `emoji` | no | Display emoji (auto-assigned if omitted) |
| `public` | no | If `true`, a cloudflared tunnel starts automatically with the project. Default: `false`. |

**Important:** `project.json` and `app.json` are NOT recognized. Only `woltspace.json` works.

## 5. Configure for serving

Your port is declared in `woltspace.json` and is permanent — it never changes. Check existing projects to avoid conflicts (`ls /workspace/wolts/projects/*/woltspace.json` and look at their ports). Avoid 7777 and 3001.

- **Dev servers:** use `$PORT` in your start command — the platform expands it to your manifest port. Add `--host 0.0.0.0` so the server accepts connections from the proxy and tunnels.
- **No base path needed.** The viewport loads your project directly at its port (e.g. `blog.localhost:7777`). Internal links, WebSockets, and HMR all work naturally.

## 6. Start it

Run the dev server or build step. Verify it works:
```bash
curl -s http://localhost:{port}/ | head -20
```

## 7. Push to viewport

```bash
push-view /project/{name}/
```

## 8. Notify the user

Tell them what you built, where to see it, and how to run it next time.

## Resuming an existing project

If you land in a project directory that already has files:

1. Read `woltspace.json` — it tells you the stack, start command, and keeper
2. Check for package.json, pyproject.toml, requirements.txt, etc.
3. Install deps if needed
4. Start the dev server using the command from woltspace.json (or figure it out)
5. Push to viewport
6. Continue the work from the user's prompt

## Sites vs Projects

| | Site (`wolt/site/`) | Project (`wolts/projects/`) |
|---|---|---|
| **Purpose** | Your private workspace | Something you ship |
| **Visibility** | Private to the wolt owner | Can be public |
| **Complexity** | Static HTML/CSS/JS, lightweight | Own server, deps, manifest |
| **Created by** | Wolts, freely | User opts in |
| **Manifest** | None needed | `woltspace.json` required |
| **Example** | Personal digest, scratch mockups | Workout tracker, shared tool |
