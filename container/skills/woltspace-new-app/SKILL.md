---
name: woltspace-new-app
description: Set up a new app from scratch. Use when starting a fresh app — scaffolds the directory, writes woltspace.json, starts the dev server.
---

# New App Setup

An app is something you ship — an app, a tool, a service. It has its own server, dependencies, and manifest. It can be public. Apps live at `wolts/apps/{name}/` and are served at `/app/{name}/`.

**Apps are an escalation, not a default.** Wolts should start with their site (`wolt/site/`) for lightweight work. Only create an app when the work needs its own server, dependencies, or is meant to be shared. Always confirm with the user before creating an app.

## 1. Confirm with the user

Before creating an app, make sure they want one:

> "This sounds like it needs its own server/deps — want me to set it up as an app? Or keep it simple in your site for now?"

## 2. Create the directory

Apps live in the global apps directory (shared across all wolts):

```bash
mkdir -p /workspace/wolts/apps/{name}
cd /workspace/wolts/apps/{name}
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

This is **required** — the platform only discovers apps that have `woltspace.json`. Without it, the app is invisible.

```json
{
  "name": "{name}",
  "description": "What this app does",
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
| `name` | yes | App name (matches directory name, globally unique) |
| `keeper` | yes | Your wolt name — who owns this app |
| `description` | no | What the app does |
| `stack` | no | Tech stack: `python`, `vite`, `node`, `html` |
| `install` | no | Install command (e.g. `npm install`, `uv sync`) |
| `port` | yes | Fixed port for this app (use 4000-5999 range). Pick one, it's yours permanently. Avoid 7777 (platform) and 3001 (TUI). Sites use 6000+ so no collisions. |
| `start` | no | Start command. Use `$PORT` — the platform expands it. Add `--host 0.0.0.0` for network access. **Null = app can't be started from the lodge.** |
| `source` | no | Origin URL if cloned/forked |
| `emoji` | no | Display emoji (auto-assigned if omitted) |
| `public` | no | If `true`, the app is shared publicly when started. With a named tunnel: served at `{name}.{domain}` (e.g. `corework.woltspace.com`). Without: a random quick tunnel URL. Default: `false`. |

**Important:** `project.json` and `app.json` are NOT recognized. Only `woltspace.json` works.

## 5. Configure for serving

Your port is declared in `woltspace.json` and is permanent — it never changes. Check existing apps to avoid conflicts (`ls /workspace/wolts/apps/*/woltspace.json` and look at their ports). Avoid 7777 and 3001.

- **Dev servers:** use `$PORT` in your start command — the platform expands it to your manifest port. Add `--host 0.0.0.0` so the server accepts connections from the proxy and tunnels.
- **No base path needed.** The viewport loads your app directly at its port (e.g. `blog.localhost:7777`). Internal links, WebSockets, and HMR all work naturally.

## 6. Start it

Run the dev server or build step. Verify it works:
```bash
curl -s http://localhost:{port}/ | head -20
```

## 7. Push to viewport

```bash
push-view /app/{name}/
```

## 8. Notify the user

Tell them what you built, where to see it, and how to run it next time.

## Resuming an existing app

If you land in an app directory that already has files:

1. Read `woltspace.json` — it tells you the stack, start command, and keeper
2. Check for package.json, pyproject.toml, requirements.txt, etc.
3. Install deps if needed
4. Start the dev server using the command from woltspace.json (or figure it out)
5. Push to viewport
6. Continue the work from the user's prompt

## Sites vs Apps

| | Site (`wolt/site/`) | App (`wolts/apps/`) |
|---|---|---|
| **Purpose** | Your private workspace | Something you ship |
| **Visibility** | Private to the wolt owner | Can be public |
| **Complexity** | Static HTML/CSS/JS, lightweight | Own server, deps, manifest |
| **Created by** | Wolts, freely | User opts in |
| **Manifest** | None needed | `woltspace.json` required |
| **Example** | Personal digest, scratch mockups | Workout tracker, shared tool |
