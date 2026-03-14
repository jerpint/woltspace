# Woltspace — Developer Guide

## What is this?

**Woltspace** is a platform for running autonomous AI agents called **wolts**. Each wolt is:
- An AI with a persistent identity, memory, and personality
- Living inside a Docker container (the "space") where it can build whatever it wants
- Connected to its human partner via messaging apps (Telegram, WhatsApp/Slack)

The human chats with the wolt via Telegram. The wolt can spin up Claude Code sessions to do real work — write code, build websites, generate content. The human can watch the work happen live via a split-view browser UI (terminal on left, viewport on right).

**This repo is the platform** — the Docker image, server, bot brain, and CLI that makes wolts run. Individual wolt repos live separately (under `~/wolts/{name}/`).

> **⚠️ Platform code is immutable from wolt sessions.** Wolts must NEVER edit files in `/workspace/woltspace/`. All wolt work happens inside their own directory (`/workspace/wolts/{name}/`). Code projects go in `wolt/projects/`. If a wolt needs new platform functionality, the human files an issue — wolts don't patch the platform.

---

## Architecture at a Glance

```
Human (Telegram/Slack)
    ↓
Python bot (litellm → Claude/Haiku)
    ↓
Tool: claude_code → tmux session → Claude Code CLI
    ↓
Wolt builds things (site, sparks, artifacts)
    ↓
Node.js server → split view in browser ← Human watches
    ↑
cloudflared tunnel (public URL, no account needed)
```

---

## Lore — The Colony

**Woltspace** is the whole thing. Wolts live and work here.

**The lodge** — home base. Always-on, handles day-to-day chat.

**The dens** — where real work happens. Claude Code sessions, spawned per task, dissolved when done.

**The pond** — the visible surface (viewport). Wolts build in dens, surface work to the pond.

### The Animals

All three are wolts. The animal describes the model, tempo, and role.

**🦦 Otterwolt — Haiku**
Quick, social, lives at the water's edge. Handles incoming signals, routes fast, always present. First to hear, first to respond. Bot responses formatted as `🦦 name: text`.

**🦫 Beaverwolt — Sonnet**
The builder. Industrious, shapes the environment, makes things that last. Den sessions run as beaverwolts — this is where real work happens. Den responses prefixed `🦫`.

**🦝 Raccoonwolt — Opus**
The orchestrator. Clever, adaptive, trickster wisdom. Sees the whole pond from the water's edge — doesn't build everything itself but knows where everything fits. Reaches across the whole colony when needed.

```
woltspace
  lodge           — home base
    otterwolt 🦦  — always-on, quick response, entry point
    beaverwolt 🦫 — builder with memory and identity
    raccoonwolt 🦝 — orchestrator, spans the colony
    ... other creatures may find roles here eventually
  dens            — temporary work sessions (where beaverwolts build)
  pond            — the visible surface (viewport)
```

---

## Key Components

### `woltspace` (bash CLI)
The host-side CLI. Commands:
- `woltspace init` — create a new wolt from template
- `woltspace start` — start the container
- `woltspace stop/restart/rebuild/shell/logs`

Reads `.env` for secrets, mounts all wolts into the container.

### `server.js` (Node.js, ~900 lines)
Single-file HTTP + WebSocket server running on port 3000 inside the container.
- Serves the split view (`/tui?session=X`) — xterm.js terminal + iframe viewport
- Manages per-session viewport URLs (`/current`)
- Serves static files: `public/` (platform UI) → `wolt/site/` → `wolt/sparks/`
- Proxies tool registrations at `/tools`
- Serves apps at `/app/:name/` (static from `dist/` or proxy to port — see `apps` skill)
- Runs the digest cron (6am + 3pm)
- Live reload via SSE at `/livereload`

### `container/bot/core.py` (Python)
The bot brain. Loaded by Telegram/Slack adapters. Uses **litellm** for LLM routing.
- Builds system prompt from wolt memory files
- Defines tools: `claude_code`, `new_session`, `get_tunnel_url`, `check_session`, `get_recent_sessions`, `list_sessions`, `find_session`, `kill_session`, `send_message`, `read_memory`, `list_wolts`, `list_projects`, `generate_image`, `switch_wolt`
- When `claude_code` is called: spawns a tmux session running `run-session.sh` → Claude Code CLI
- Session metadata (status, routing, creature, viewport) stored in the **session registry** — see `container/lib/sessions.py`

### `container/bot/telegram_adapter.py`
Thin Telegram layer over core. Persists chat history to `.state/chat/{chat_id}.jsonl`. Group chat support (responds when @mentioned).

### `container/Dockerfile` + `container/entrypoint.sh`
Image based on `node:22-slim`. Installs: cloudflared, uv, Claude Code CLI, tmux.
The Dockerfile uses a multi-stage build: Claude Code CLI is installed in an isolated `claude` stage, cached independently of app source changes. To update Claude without rebuilding everything: `docker build --no-cache-filter=claude ...`
Entrypoint:
1. Merges platform skills + wolt overrides into `~/.claude/skills/`
2. Writes OAuth credentials and trust config
3. Starts tmux `main` session with Claude Code auto-running
4. Starts Node server with `--watch`
5. Starts cloudflared tunnel (URL written to `.state/tunnel-url`)
6. Optionally starts Telegram/Slack bot

### `container/skills/`
Discovery files Claude Code reads from `~/.claude/skills/`. Platform defaults baked into image; wolts can override. Current skills: `apps`, `projects`, `new-project`, `migrate-to-projects`, `create-wolt`, `digest`, `music`, `viewport`, `telegram`, `notify`, `session-summary`, `organize-context`.

### `container/cron/digest.mjs`
Daily digest pipeline (3 phases): fetch (HN, HuggingFace, Lobsters) → select via `claude -p` → render HTML. Writes to `wolt/sparks/`. Optional Spotify playlist curation.

### `container/lib/sessions.py` (Session Registry)
Centralized session metadata — single source of truth replacing the old scattered `sessions/*.json` + `session-routing/*.json` files. One JSON file per session in `.state/registry/{session_name}.json`.

Each entry tracks: name, wolt, creature, model, status, timestamps, routing (adapter, chat_id, user_id, thread_ts), viewport URL, session URL.

API: `SessionRegistry.create()`, `.update()`, `.get()`, `.list()`, `.finish()`, `.touch()`, `.reconcile()`, `.delete()`.

Used by core.py, run-session.sh, notify, and server.js. CLI wrapper: `session-reg`.

### `container/bin/`
Utility scripts available in container PATH:
- `push-view <path>` — set viewport URL for current session
- `run-session.sh` — wrapper that sends ack, injects notification context, runs Claude Code
- `notify` — send message back to originating adapter (Telegram/Slack)
- `session-reg` — CLI for the session registry (`session-reg list`, `session-reg get <name>`, `session-reg reconcile`)
- `spawn-tool` — register a tool proxy with the server
- `migrate-sessions-to-registry.sh` — one-time migration from old session files to registry format

---

## ⚠️ VIEWPORT — HOW TO SHOW THINGS TO THE HUMAN

**The right-hand pane of the split view is the viewport. Use it. Always push what you build.**

The `/viewport` skill explains everything, but the short version:
1. Write your HTML/app to `wolt/site/` (or build an app under `wolt/apps/`)
2. Run `push-view /your-page.html` — this updates the right pane live
3. The human sees it immediately in their browser

`push-view` auto-detects the current session. **If you build something and don't push it to the viewport, the human can't see it.**

Use `/viewport` for full details: URL paths, app serving, live-reload behavior.

---

## Wolt Directory Structure (per wolt repo)

```
~/wolts/{name}/
  wolt/
    memory/
      identity.md      — personality, values, voice (full load)
      context.md       — current state, open threads (first 80 lines)
      learnings.md     — active patterns (first 40 lines)
      index.md         — memory index for discoverability
      archive/         — grows forever, searched on demand
    projects/          — isolated code projects (apps, scripts, experiments)
    apps/              — full-stack apps (each has app.json, served at /app/:name/)
    site/              — static HTML/CSS public space
    sparks/            — generated artifacts
    drafts/
  .claude/             — Claude Code auth + session state
  .state/              — runtime (tunnel URL, session registry)
    registry/          — one JSON file per session (status, routing, metadata)
  .env                 — secrets (gitignored)
  CLAUDE.md            — wolt-specific instructions
  wolt.json            — manifest
```

**Isolation boundary:** Sessions run inside the wolt directory. All code edits MUST stay within this directory. The platform (`/workspace/woltspace/`) is read-only to wolts.

---

## Multi-Wolt Setup

Multiple wolts can run in one container. They all share:
- The same Node server
- The same bot process
- `~/wolts/.state/registry/` for session metadata and notification routing
- `~/wolts/.claude/` for Claude Code auth

`~/wolts/woltspace.json` tracks the active wolt per adapter. The bot can `switch_wolt` at runtime.

---

## Communication Channels

Currently supported: **Telegram**, **Slack**. WhatsApp is planned.

Each adapter is a thin Python file over `core.py`. To add a new adapter: copy `telegram_adapter.py`, implement message send/receive, set `BOT_ADAPTER` env var, start it in `entrypoint.sh`.

---

## Key Environment Variables

```bash
WOLT_NAME=alice           # which wolt to boot
HUMAN_NAME=alice's human  # used in bot system prompt
CLAUDE_CODE_OAUTH_TOKEN=  # auth for Claude Code CLI
ENABLE_TELEGRAM_BOT=true
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_USERS=   # comma-separated IDs, or empty for open
ENABLE_SLACK_BOT=false
LLM_MODEL=anthropic/claude-haiku-4-5-20251001  # bot model
ENABLE_DIGEST_CRON=false
ENABLE_TUNNEL=true
```

---

## Testing

Tests live in `test/` and run via `test/run-tests.sh`. The runner sources `/workspace/wolts/.env` for secrets automatically.

```bash
# From /workspace/woltspace:
bash test/run-tests.sh              # all tests (103 tests, ~2 min)
bash test/run-tests.sh unit         # pure Python, no server/tmux needed
bash test/run-tests.sh integration  # requires running server + tmux
bash test/run-tests.sh closed-loop  # full chain: telegram + server + tmux + registry
bash test/run-tests.sh agent        # haiku in the loop (costs API tokens)
bash test/run-tests.sh live         # requires TELEGRAM_BOT_TOKEN (hits real API)
bash test/run-tests.sh -k "pattern" # pass any pytest args
```

**Test files:**
- `test_bot_core.py` — pure unit tests: session naming, ack text, creature routing, command building, history sanitization, memory loading
- `test_session_lifecycle.py` — session registry CRUD + tmux session management
- `test_server_health.py` — HTTP endpoint health checks (requires server)
- `test_telegram_loop.py` — Telegram adapter: message parsing, formatting, allowlist, notify round-trip, live API
- `test_closed_loop.py` — full seam tests: Telegram API → notify pipeline → session creation → den reply → round-trip
- `test_agent_loop.py` — haiku decision tests (mocked tools), conversation simulator, live session spawn, true e2e (haiku → beaver → file on disk → viewport)

**Environment:**
- `TEST_VERBOSE=1` (default) — posts per-test results to Telegram test group
- `TEST_VERBOSE=0` — summary only
- `TEST_CHAT_ID` — dedicated test group chat (set in `.env`)
- `KEEP_E2E_ARTIFACTS=1` — preserve hello-wolt-test.html and session after e2e test

**Dependencies:** `uv sync --project container/bot/pyproject.toml` (auto-installed by runner)

---

## Development

This repo is usually mounted into the container in dev mode (`woltspace start` auto-detects if you're in the repo and enables dev mode). The server runs with `node --watch` — save `server.js` and it restarts. The bot does NOT auto-restart; kill and relaunch it manually after edits.

Restart Telegram bot:
```bash
pkill -f telegram_adapter
set -a && source /workspace/wolts/${WOLT_NAME}/.env && set +a
cd /workspace/woltspace/container && uv run --project bot/pyproject.toml python -m bot.telegram_adapter &
```

---

## Known Bugs / TODO

- **`_send_ack` only handles Telegram** (`container/bot/core.py`) — Slack users get no "🦫 on it" ack when a session spawns. Easy fix: add the Slack API call alongside the Telegram one.
- **`logger` defined twice** (`container/bot/core.py`) — harmless but one is dead code.
- **`container/bin/spawn-tool` is untracked in git** — new file sitting uncommitted.

## What's Messy / Still Iterating

- **WhatsApp adapter**: not yet implemented
- **Multi-wolt UX**: switching wolts works but UI is minimal
- **Old session files**: `.state/sessions/` and `.state/session-routing/` are superseded by `.state/registry/` — can be deleted once confirmed stable
- **Digest cron**: timezone handling is approximate
- The split view UI (`public/split.html`) has grown organically — could use cleanup
- `agents.md` in the repo root is older technical reference, may be out of date
- `site/llms.txt` is a stub from earlier, not used by agents in current architecture
