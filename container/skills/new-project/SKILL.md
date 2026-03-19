---
name: new-project
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
  "install": "npm install",
  "start": "node server.js",
  "keeper": "{your-wolt-name}",
  "emoji": "🦊"
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
| `start` | no | Start command. **Null = project can't be started from the lodge.** |
| `source` | no | Origin URL if cloned/forked |
| `emoji` | no | Display emoji (auto-assigned if omitted) |

**Important:** `project.json` and `app.json` are NOT recognized. Only `woltspace.json` works.

## 5. Configure for serving

Pick a port in the 4001-4999 range (auto-allocated by the platform when started from lodge). If it's a framework with a dev server:

- **Static builds:** set the base path to `/project/{name}/` (Vite: `base`, Astro: `base`, Next: `basePath`)
- **Dev servers:** just run on the allocated port — the platform sets `PORT` env var when starting

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
