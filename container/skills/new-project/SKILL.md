---
name: new-project
description: Set up a new project from scratch. Use when starting a fresh project — scaffolds the directory, detects the stack, starts the dev server, and writes project.json.
---

# New Project Setup

You're setting up a new project. Follow these steps:

## 1. Confirm you're in the right place

You should be in `wolt/projects/{name}/`. If not, create the directory and cd into it:

```bash
mkdir -p wolt/projects/{name}
cd wolt/projects/{name}
```

## 2. Determine what to build

Based on the user's request, figure out:
- What kind of project (web app, API, static site, script, etc.)
- What language/framework makes sense
- Whether to clone an existing repo or start fresh

## 3. Scaffold it

Some common starting points:

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

## 4. Configure for serving

Pick a port (4001-4999, avoid conflicts with other projects). If it's a framework with a dev server:

- Set the base path to `/project/{name}/` for static builds
- Or just run the dev server on your chosen port for proxy mode

## 5. Start it

Run the dev server or build step. Verify it works:
```bash
curl -s http://localhost:{port}/ | head -20
```

## 6. Write project.json

This is your receipt — documenting what you set up so the next session can pick it up:

```json
{
  "name": "{name}",
  "port": 4001,
  "start": "npm run dev -- --port 4001",
  "language": "javascript",
  "framework": "vite-react",
  "description": "What this project does"
}
```

Only `port` is used by the platform. Everything else is for the next beaver/raccoon.

## 7. Push to viewport

```bash
push-view /project/{name}/
```

## 8. Notify the user

Tell them what you built, where to see it, and how to run it next time.

## Resuming an existing project

If you land in a project directory that already has files:

1. Read `project.json` if it exists — it tells you what the last session set up
2. Check for package.json, pyproject.toml, requirements.txt, etc.
3. Install deps if needed
4. Start the dev server using the command from project.json (or figure it out)
5. Push to viewport
6. Continue the work from the user's prompt
