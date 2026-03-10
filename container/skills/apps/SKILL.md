---
name: apps
description: Build and serve full-stack apps from within the container. Use when the user wants to create a web app, API, dashboard, blog, or any project that needs its own routes.
---

# Apps — Full-Stack App Serving

Apps live at `wolt/apps/{name}/` and are served at `/app/{name}/`. One route, two modes: static files from `dist/`, or reverse proxy to a running server.

## Quick start

### Static app (Astro, Vite, Next export, any build tool)

```bash
# 1. Create the app
mkdir -p wolt/apps/blog && cd wolt/apps/blog
npm create astro@latest . -- --template blog

# 2. Write app.json (required — this registers the app)
cat > app.json << 'JSON'
{"name": "blog"}
JSON

# 3. Configure the base path — CRITICAL for assets/links
#    Astro: base: '/app/blog/' in astro.config.mjs
#    Vite:  base: '/app/blog/' in vite.config.js
#    Next:  basePath: '/app/blog' in next.config.js

# 4. Build
npm run build   # outputs to dist/

# 5. Push to viewport
push-view /app/blog/
```

### Server app (Express, Fastify, any backend)

```bash
# 1. Create the app
mkdir -p wolt/apps/api && cd wolt/apps/api

# 2. Write app.json with a port
cat > app.json << 'JSON'
{"name": "api", "port": 4001}
JSON

# 3. Write your server (it sees stripped paths — / not /app/api/)
cat > server.js << 'JS'
import express from 'express';
const app = express();
app.get('/', (req, res) => res.json({ status: 'ok' }));
app.listen(process.env.PORT || 4001);
JS

# 4. Start it
PORT=4001 node server.js &

# 5. Test
curl http://localhost:3000/app/api/
```

## The protocol

### `app.json` (required)

Every app needs `wolt/apps/{name}/app.json`. Minimum:

```json
{"name": "myapp"}
```

For server apps, add the port:

```json
{"name": "myapp", "port": 4001}
```

Without `app.json`, the `/app/{name}/` route returns 404. This is the gate — it prevents random directories from being served.

### How the server decides what to do

1. Match `/app/{name}/*`
2. Read `wolt/apps/{name}/app.json` — 404 if missing
3. If `dist/` directory exists → **serve static files** from it
4. Else if `port` in app.json → **reverse proxy** to `localhost:{port}`
5. `dist/` always wins (build = production, port = dev)

### Path handling

- **Static:** `/app/blog/about` → reads `wolt/apps/blog/dist/about` or `dist/about/index.html`
- **Proxy:** `/app/api/users/123` → forwards as `/users/123` to `localhost:{port}` (prefix stripped)
- **WebSocket:** proxy mode supports WS upgrade (HMR, live-reload, realtime)

### Base path rule

**Static apps must configure their framework's base path to `/app/{name}/`.** This ensures all asset URLs, links, and routes work correctly when served under the prefix.

| Framework | Config |
|-----------|--------|
| Astro | `base: '/app/{name}/'` in `astro.config.mjs` |
| Vite | `base: '/app/{name}/'` in `vite.config.js` |
| Next.js | `basePath: '/app/{name}'` in `next.config.js` |
| SvelteKit | `paths.base: '/app/{name}'` in `svelte.config.js` |

**Server apps don't need a base path.** The proxy strips `/app/{name}` before forwarding, so your server sees clean paths (`/`, `/api/foo`, etc.).

## Pushing to the viewport

```bash
push-view /app/myapp/
```

The viewport iframe loads `/app/myapp/` from the same origin — no CORS issues.

## Making it public

Once an app is served at `/app/{name}/`, anyone with the tunnel URL can reach it at `https://{tunnel}/app/{name}/`. No extra config needed — the `/public/:session` endpoint is for the live session view; apps are directly accessible.

## Listing apps

```bash
curl http://localhost:3000/apps
```

Returns JSON array of all registered apps with their mode (static/proxy).

## Resource awareness

Apps run inside the container alongside Claude Code sessions, the bot, and the server. Be mindful:

- **Don't run heavy databases** (Postgres, MySQL) — use SQLite or files
- **One dev server at a time** is fine; multiple concurrent servers eat RAM
- **Always build for production** when the app is ready (`npm run build`), then stop the dev server
- If an app needs a separate service (Redis, etc.), start it only when needed and stop it after

## Troubleshooting

- **404 on `/app/myapp/`** → check `wolt/apps/myapp/app.json` exists
- **Assets 404 / broken links** → check the framework base path is set to `/app/{name}/`
- **502 on proxy** → the server process isn't running; start it on the port in `app.json`
- **Blank page in viewport** → check the browser console; usually a base path issue
