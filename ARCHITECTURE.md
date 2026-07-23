# Woltspace Architecture

> A platform for autonomous AI agents ("wolts") that live in Docker containers with persistent identity, memory, and the ability to build real things. Humans interact via Telegram/Slack or a browser; the wolt works in Claude Code sessions and pushes output to a split-view UI.

This is the canonical reference for how the platform is shaped. It describes services, how they fit together, and where to read for more — not specific issue numbers, line counts, or release-bound details. Those live in git, GitHub, and `CHANGELOG.md`.

---

## The three layers

```
┌─────────────────────────────────────────────────┐
│            COMMUNICATION LAYER                   │
│     Telegram adapter  ·  Slack adapter           │
│     (thin async wrappers over bot core)          │
└────────────────────┬────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────┐
│            ORCHESTRATION LAYER                    │
│     Bot core (Haiku via litellm)                 │
│     Agent loop · tool registry · memory loading  │
└────────────────────┬────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────┐
│            EXECUTION LAYER                        │
│     FastAPI server · tmux + Claude Code          │
│     Split-view browser UI · Cloudflare tunnel    │
└─────────────────────────────────────────────────┘
```

- **Communication** — adapters in `container/bot/` translate platform-specific events (Telegram updates, Slack events) into the shared message shape the bot core expects.
- **Orchestration** — `container/bot/core.py` is the brain. Haiku drives an agent loop with a registry of tools (spawn a Claude session, check status, send messages, list wolts, schedule a wolf, etc.). The full set of tools lives in `TOOL_HANDLERS` and `TOOLS` in `core.py`; treat that as canonical.
- **Execution** — the FastAPI server in `server/app.py` is the central authority. It serves the lodge UI, brokers viewport state, runs the subdomain proxy, manages tunnels, and exposes everything via HTTP/WebSocket. tmux sessions running Claude Code are spawned and tracked through the session registry.

---

## Repo layout

```
server/                     FastAPI server, port 7777
  app.py                      All HTTP/WebSocket routes; subdomain proxy middleware
  config.py                   Paths, env, MIME types — one place for constants
  state.py                    Viewport state + views history
  tunnel.py                   Lodge tunnel lifecycle (quick or named)
  notify.py                   Outbound Telegram/Slack notifications
  tools.py                    Tool process registry (spawn + proxy)
  sparks.py                   Spark/digest storage
  tui-service.js              Node pty + WebSocket — the only Node piece left

container/
  Dockerfile                  Image: node:22-slim + claude CLI + python deps + cloudflared
  entrypoint.sh               Root wrapper — fixes UID/GID to host, drops to node user via gosu
  start.sh                    Boots tmux + Claude, FastAPI, tunnel, bots, watcher
  entrypoint_setup.py         Resolves config + identity + env before start.sh

  bot/                        Orchestration layer
    core.py                     Agent loop, tool registry, memory loading
    telegram_adapter.py         Telegram handler (text, voice, photos, den replies, history, pickers)
    slack_adapter.py            Slack handler (Socket Mode, threads, @mention routing)
    image_gen.py                AI image gen wrapper

  lib/                        Shared building blocks (importable by server/ and bot/)
    sessions.py                 SessionRegistry + start_session — one source of truth
    apps.py                     App schema + discovery + start/stop + port allocation
    wolts.py                    Wolt discovery + creature types + create_creature_wolt
    sites.py                    Per-wolt static sites + livereload + permanent ports
    tunnel.py                   Cloudflared helpers (quick + named)
    paths.py                    Per-wolt and global path helpers — start here when locating state

  bin/                        Scripts on PATH inside the container
    notify, push-view           Wolt-facing helpers (notify the user, set the viewport)
    wclaude, run-session.sh     Session entry — wraps `claude` with notify hooks + identity
    gh-app-token                Mints short-lived GitHub App installation tokens
    version-check               Compares stamped version to upstream releases

  hooks/                      Claude Code hooks (notify.sh, session-done.sh, run-session.sh)
  cron/                       Scheduled scripts (e.g. check-update.sh)
  creatures/                  Per-creature behavior modules (wolf, dog, vulture, …)
  skills/                     Platform skills exported into every wolt

templates/                  Jinja2 — base.html, home.html (lodge), tui.html (split view)
public/                     Static assets — onboard.html, favicon, sw.js, static/ (CSS/JS/sprites)
test/                       Pytest suite (run with `uv run --extra test pytest test/`)
migrations/                 Per-version migration scripts (see VERSIONING.md)
```

---

## How a message becomes work

### Inbound: human → wolt

```
User sends "build me a homepage"
  → telegram_adapter.handle_message()              (or slack_adapter)
  → core.get_response()                            Haiku agent loop
  → Haiku picks a tool: claude_code
  → core.start_claude_session()
      ├─ registry.create()                         wolts/{wolt}/.state/sessions/{name}.json
      ├─ tmux new-session                          spawns run-session.sh
      └─ returns {name, url, creature}
  → adapter sends ack to Telegram/Slack
```

The agent loop is multi-turn — Haiku can chain tool calls. Tools live in `core.py`'s `TOOL_HANDLERS` dict; their JSON schemas (the contract Haiku sees) live in the `TOOLS` list right below.

### Notify back: Claude → user

```
Claude in session calls: notify "done, check it out"
  → hooks/notify.sh                                Claude Code Notification hook
  → POST /notify on FastAPI
  → server reads session routing from registry
  → adapter sends to Telegram/Slack with footer:
        "↩ reply to this message…"
        "https://tunnel/tui?session=NAME"
```

### Reply: user → existing session

```
User replies in Telegram to a 🦫 message
  → adapter detects reply-to-notify (footer present)
  → extracts session name from URL in the footer
  → core.message_session(name, text)
      ├─ if Claude is running:   tmux paste-buffer + Enter
      └─ if exited:              revive via `claude --continue`
```

The "den reply" path bypasses Haiku — the user's message goes straight to the running Claude session. Reply routing is what lets long conversations stay coherent without polling Haiku in between.

### Viewport updates

```
Claude pushes a view
  → POST /current?session=X {url: "/index.html"}
  → server writes viewport_url into session JSON

split view polls /current/meta
  → iframe loads the new URL
  → server injects livereload script into HTML
  → file changes trigger instant reload via /livereload WebSocket
```

The session JSON is the source of truth for what's in the viewport — no separate "current viewport" file.

---

## Sessions

A session is a Claude Code conversation with identity (which wolt, which creature, which model) and routing (where to send notifications).

- **Registry** — one JSON file per session at `wolts/{wolt}/.state/sessions/{name}.json`. Filesystem is the database; `ls` and `cat` are valid queries.
- **Naming** — `{wolt}-{adjective}-{noun}-{6hex}` (e.g. `neowolt-chompy-dam-a3f1e2`).
- **Creatures** — `otter` (haiku), `beaver` (sonnet), `raccoon` (opus). The creature picks the model; the role of the session is otherwise the same.
- **Multi-adapter routing** — `routing` is an array, so a session can be started on Slack, picked up in the browser, and notified on Telegram.
- **Lifecycle** — `CREATED → RUNNING → COMPLETED/FAILED`, with `ORPHANED` if tmux dies without firing the exit hook. `registry.reconcile()` checks tmux liveness and marks dead sessions; the vulture creature reaps stale tmux sessions on a schedule.
- **Resume** — `claude --resume {uuid}` via the stored `claude_session_id`. See `container/lib/sessions.py` for the canonical resume command builder.

All session machinery lives in `container/lib/sessions.py`. `start_session()` is the single entry point — every code path that creates a session goes through it.

---

## Sites and apps

Two execution surfaces, one server.

The lodge UI is also shared across the browser, installed PWA/mobile, and the
macOS native shell. See [Client surfaces and navigation](docs/client-surfaces.md)
for the cross-client ownership and navigation contract.

- **Sites** (`wolts/{wolt}/site/`) — lightweight per-wolt workspace. Static HTML/CSS/JS served by the FastAPI server itself, with livereload baked in via an injected client. Each wolt gets a permanent port stored in its `wolt.json`. Code: `container/lib/sites.py`, served at `/wolt/{name}/site/`.
- **Apps** (`wolts/apps/{name}/`) — full programs with their own server, deps, and `woltspace.json` manifest. The server starts/stops them; their ports are tracked in `.space/apps/`. Apps that set `public: true` get a Cloudflare tunnel automatically and survive container restarts via apps autorestore. Code: `container/lib/apps.py`.

### Subdomain proxy

`server/app.py` has an HTTP middleware (`subdomain_proxy`) that routes `*.localhost` and `*.{tunnel_domain}` requests to the right app port. `corework.woltspace.com` → look up the running app named `corework` → proxy to its localhost port. This avoids path-prefix headaches for multi-page apps and is what makes per-app public URLs cheap.

The proxy streams responses (so SSE / Vite HMR / video Range requests work) and preserves `Content-Length` for partial-content responses. WebSockets get the same treatment via a catch-all WS route.

---

## Tunnels

Two flavors, both in `container/lib/tunnel.py`:

- **Quick tunnel** — `cloudflared tunnel --url http://localhost:7777`. Random `*.trycloudflare.com` URL, parsed from cloudflared's logs. No account needed. Default for new installs.
- **Named tunnel** — `cloudflared tunnel run --token $TOKEN`. Permanent URL on a domain you control, configured via Cloudflare's API. Set `CLOUDFLARE_TUNNEL_TOKEN` + `CLOUDFLARE_TUNNEL_URL` in `.env`. `WOLTSPACE_PUBLIC_TUNNEL=false` disables tunneling entirely.

Selection is config-driven; `server/tunnel.py` picks the right path at startup. The same module also tracks `tunnel_domain` and `tunnel_hostname` so the subdomain proxy knows which host is the lodge and which are app subdomains.

---

## Onboarding

A new install with no wolt and no auth boots into "onboard mode": the server falls back to `public/onboard.html`, the tmux main session runs bare `claude /login`, and there is no active wolt. Once the user authenticates and creates a wolt (lodge UI or `/woltspace-create-wolt`), normal mode kicks in: viewport defaults to the new wolt's site, Claude relaunches under that wolt's identity, and the bot adapters can start.

The onboard fallback lives in `server/state.py` and the boot branching in `container/start.sh`.

---

## State model

```
wolts/                                     mounted into the container
├─ {wolt}/                                 per wolt
│   ├─ wolt/                               wolt-owned content (memory, site, drafts, …)
│   │   ├─ memory/                         identity.md, context.md, learnings.md (+ archive/)
│   │   ├─ site/                           the wolt's static site
│   │   ├─ sparks/                         generated artifacts
│   │   └─ drafts/                         writing
│   ├─ .claude/                            skills, hooks, OAuth credentials
│   ├─ .state/                             runtime state
│   │   ├─ sessions/                       one JSON per session
│   │   ├─ site.json                       livereload port + pid
│   │   └─ wolf/                           cron execution state
│   ├─ .env                                wolt secrets
│   ├─ CLAUDE.md                           wolt-specific instructions
│   └─ wolt.json                           manifest (name, type, role, site_port, …)
│
├─ apps/{name}/                            shipped apps
│   ├─ woltspace.json                      manifest (start, port, public, …)
│   └─ …                                   app source
│
└─ .space/                                 global state (no single wolt owns it)
    ├─ platform/                           tunnel.json, version, branch
    ├─ apps/                               running app port + pid files
    ├─ wolf/                               wolf scheduler
    ├─ vulture/                            session reaper
    └─ logs/                               bot.jsonl event log
```

The split between `wolts/{wolt}/` (owned by one wolt) and `wolts/.space/` (cross-wolt) is the core of the state model. `container/lib/paths.py` is the canonical map — start there if you're not sure where to read or write.

---

## Conventions

- **Single entry points** — `start_session()` for sessions, `start_app()` for apps, `start_site()` for sites. Don't reimplement; import.
- **Filesystem as database** — paths are queries, files are records, dirs are indexes. Cleanup is `rm`.
- **FastAPI is the central authority** — it owns reads, writes, and policy. Adapters ask the server, not the filesystem.
- **`WOLT_DIR` is for code, not state** — runtime state always goes through `.state/` or `.space/`.
- **`/workspace/woltspace/` is baked into the image, not mounted** — only `/workspace/wolts/` is mounted at runtime. Container-lifecycle changes (Dockerfile, entrypoint, baked deps) require `woltspace rebuild`; everything else is hot-editable in dev mode.
- **Skills layer** — platform defaults in `container/skills/`, wolt-specific overrides in `wolts/{wolt}/.claude/skills/`. Wolt overrides win.

For tests, see `test/` (`uv run --extra test pytest test/`). For release process, see `VERSIONING.md`. For history, see `CHANGELOG.md` and `git log`.
