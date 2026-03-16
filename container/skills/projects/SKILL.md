---
name: projects
description: Work with projects — the isolated workspace for building apps, scripts, and experiments. Use when creating, running, or managing a project.
---

# Projects — Isolated Workspaces

Projects live at `wolt/projects/{name}/` and are served at `/project/{name}/`. Each project is self-contained — its own code, deps, and server.

## Creating a project

```bash
mkdir -p wolt/projects/my-app
cd wolt/projects/my-app
# ... set up your code
```

That's it. No manifest required upfront. Just start building.

## Serving a project

Projects are served at `/project/{name}/` automatically. Three modes:

### 1. Static files (simplest)

Just put HTML/CSS/JS files in the project directory:

```
wolt/projects/my-page/
  index.html
  style.css
```

Served at `/project/my-page/`. No config needed.

### 2. Built static (Vite, Astro, etc.)

Build to `dist/` and it gets served:

```bash
cd wolt/projects/my-app
npm run build  # outputs to dist/
```

**Important:** Set your framework's base path to `/project/{name}/`:
- Vite: `base: '/project/my-app/'` in `vite.config.js`
- Astro: `base: '/project/my-app/'` in `astro.config.mjs`

### 3. Dev server (any language/framework)

Run your own server and register the port in `project.json`:

```bash
cd wolt/projects/my-api

# Write project.json (the "receipt" — you figured out how to run it)
cat > project.json << 'JSON'
{"name": "my-api", "port": 4010, "start": "node server.js"}
JSON

# Start it
node server.js &
```

The platform proxies `/project/my-api/` → `localhost:4010`, stripping the prefix.

## project.json (optional)

The beaver/raccoon writes this after setting up a project. It's a receipt, not a requirement.

```json
{
  "name": "my-app",
  "port": 4010,
  "start": "npm run dev",
  "language": "javascript",
  "description": "A simple dashboard"
}
```

Only `port` is used by the platform (for proxying). Everything else is documentation for future sessions.

## Pushing to the viewport

```bash
push-view /project/my-app/
```

## Listing projects

```bash
curl http://localhost:7777/projects
```

## Port allocation

Use ports 4001-4999 for project servers. Each project gets its own port. Don't conflict with:
- 7777 — platform server
- 3001 — TUI WebSocket service

## Key rules

- **All code goes in projects** — don't scatter files in the wolt root
- **Never edit `/workspace/woltspace/`** — that's the platform
- **Projects are portable** — a project should work if you copy it out of woltspace
- **Write project.json after setup** — so the next session knows how to run it
- **One dev server at a time** is fine; be mindful of container resources
