# Woltspace — Developer Guide

## What is this?

**Woltspace** is a platform for running autonomous AI agents called **wolts**. Each wolt is:
- An AI with a persistent identity, memory, and personality
- Living inside a Docker container (the "space") where it can build whatever it wants
- Connected to its human partner via messaging apps (Telegram, WhatsApp/Slack)

The human chats with the wolt via Telegram. The wolt can spin up Claude Code sessions to do real work — write code, build websites, generate content. The human can watch the work happen live via a split-view browser UI (terminal on left, viewport on right).

**This repo is the platform** — the Docker image, server, bot brain, and CLI that makes wolts run. Individual wolt data lives separately (under `~/.woltspace/wolts/{name}/`).

> **⚠️ Platform code is immutable from wolt sessions.** Wolts must NEVER edit files in `/workspace/woltspace/`. All wolt work happens inside their own directory (`/workspace/wolts/{name}/`). Apps go in `wolts/apps/`. If a wolt needs new platform functionality, the human files an issue — wolts don't patch the platform.

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
FastAPI server (port 7777) → split view in browser ← Human watches
    ↑
cloudflared tunnel (optional public URL, no account needed)
```

**Key design:** The woltspace repo is `git clone`'d into the Docker image at build time. The only host mount is `~/.woltspace/wolts/` — all wolt data. The container is disposable; the wolts directory is the backup.

---

## Lore — The Colony

**Woltspace** is the whole thing. Wolts live and work here.

**The lodge** — home base. Always-on, handles day-to-day chat.

**The dens** — where real work happens. Claude Code sessions, spawned per task, dissolved when done.

**The pond** — the visible surface (viewport). Wolts build in dens, surface work to the pond.

### The Animals

Every wolt has a fixed **creature type** (`"type"` in `wolt.json`). The type determines the *default* model, tempo, and role. A wolt's type is permanent — but its engine and model can be switched (see **Engine & model** below).

**Rodents** (default) — general-purpose builders. Unlimited per workspace. Skill levels within the family:
- **🦫 Beaver — Sonnet** — the builder. Industrious, shapes the environment, makes things that last. Den sessions run as beavers — this is where real work happens.
- **🦝 Raccoon — Opus** — the orchestrator. Clever, adaptive. Sees the whole pond from the water's edge — doesn't build everything itself but knows where everything fits.
- **🦦 Otter — Haiku** — the quick one. Fast, lightweight, cheap. Great for simple lookups, one-shot edits, quick scripts.

**Singleton creatures** — one active at a time per workspace. Each is its own wolt with its own identity.
- **🐶 Dog — Haiku** — the lodge companion. Loyal, constrained, always present on Telegram. Bot responses formatted as `🐶 name: text`. Created via `/telegram` skill.
- **🐺 Wolf — Sonnet** — the scheduler. Runs crons, reminders, recurring tasks. Manages `wolt/wolf.json`. Created via `/wolf` skill.

**Creature-type rules:**
- `wolt.json` → `"type"` field: `rodent` (default), `wolf`, `dog`, `spider`, `bear`, `panda`
- `woltspace.json` → `creatures.active_wolf` / `creatures.active_dog` tracks the active singleton
- Creating a new wolf/dog demotes the old one to rodent (with notification)
- Discovery: scan `/workspace/wolts/*/wolt/wolt.json` or use `lib/wolts.py`
- CLI: `create-creature-wolt <name> <type>` creates creature-wolts

```
woltspace
  lodge           — home base
    dogwolt 🐶    — always-on, lodge companion, entry point
    beaverwolt 🦫 — builder with memory and identity
    raccoonwolt 🦝 — orchestrator, spans the colony
    otterwolt 🦦  — quick tasks, fast and lightweight
    wolfwolt 🐺   — scheduler, fires crons and dispatches creatures
  dens            — temporary work sessions (where beaverwolts/raccoonwolts/otterwolts/wolfwolts build)
  pond            — the visible surface (viewport)
```

---

## Engine & model

A wolt's creature type sets its *default* model, but the **engine** (harness) and **model**
are resolved per session and can be switched — the change applies to the wolt's *next*
session (both are frozen at spawn):

- **Engine** — `wolt.json "harness"` (e.g. `claude`, `codex`). Omit to follow the lodge default.
- **Model** — `wolt.json "model"` pins a specific model. Omit to use the tier's default.

A wolt can switch itself or a user's wolt — it's just a config edit. Valid ids come from
`GET /harnesses` (per engine: default models per tier + full selectable catalog). A pin is
engine-scoped, so an invalid one falls back to the tier default rather than breaking a spawn.
The catalog is data (`harnesses.py` seed, overridable in `woltspace.json` without code).

Full guide: `docs/switching-models.md`.

---

## Key Components

### `woltspace` (bash CLI)
The host-side CLI. A dumb pipe to Docker — no python3, no JSON parsing, no git required on host.

Commands:
- `woltspace init` — first-time setup (or reconnect existing wolts)
- `woltspace start` — start, restart, or resume container
- `woltspace stop` — stop and remove container
- `woltspace backup [tag] [--bundle]` — container-era snapshot: `docker commit` + `rsync` of the whole wolts dir (`--bundle` zips it around a `docker save`). Superseded by the native command below; kept for container users.
- `woltspace rebuild` — rebuild image + restart
- `woltspace shell/chat/logs` — interact with running container

Flags:
- `--local` — build image from local repo (COPY) instead of git clone
- `--branch <name>` — build image from a specific branch (default: main)

Env vars:
- `WOLTS_DIR` — override wolts directory (default: `~/.woltspace/wolts`)
- `WOLTSPACE_LOCAL=true` — sticky equivalent of `--local` (for dev workflows)

The only mount is `$WOLTS_DIR:/workspace/wolts`. Everything else is baked into the image.

### `src/woltspace/backup.py` — snapshots

The data-plane snapshot, in the python CLI, identical native and in-container:

- `woltspace backup [--tag T] [--out DIR]` — one `woltspace-backup-<tag>.tar.gz`
  of the wolts tree (memory, sites, sparks, apps, `woltspace.json`, `.state`,
  `.space`, `.git` history), with caches and build products excluded by name
  below the root and symlinks archived as links. Writes a
  `backup-manifest.json` into the archive, then re-opens the archive to verify
  it. Never touches docker.
- **Credentials are never archived** — `.env` at any depth,
  `.claude/.credentials.json*`, `.codex/auth.json`. A restored stale OAuth
  chain can trip reuse-detection and revoke the live one. The manifest lists
  every withheld path and `restore` prints the re-provision checklist.
- `woltspace restore <archive> [--to DIR]` — extracts into a new directory and
  refuses a populated one; prints what to re-authenticate and the `WOLTS_DIR=…`
  line to boot from it.

Full guide, including what is excluded and why: `docs/backup.md`.

### Docker Image

The Dockerfile does `git clone` of the woltspace repo during build (branch configurable via `--build-arg WOLTSPACE_BRANCH`). With `--local`, the local repo is `COPY`'d instead. Both produce the same image structure — no separate code paths.

Image based on `node:22-slim`. Installs: cloudflared, uv, Claude Code CLI, tmux, gh.

The Claude Code CLI is installed in an isolated `claude` build stage, cached independently. To update Claude: `docker build --no-cache-filter=claude ...`

### `src/woltspace/container_entrypoint.py`
Container boot, as a subcommand of the installed CLI —
`ENTRYPOINT ["/usr/local/bin/woltspace", "container-entrypoint"]`. The wheel
ships no bash. The command runs twice, dispatched on uid:

1. **root phase** — `groupmod`/`usermod` the `node` user onto `HOST_UID`/`HOST_GID`,
   re-own `/workspace`, `/home/node` and the wheel bundle, then re-exec itself
   as node through `gosu`.
2. **node phase** — scaffold the lodge and the wolt from `template/`, derive the
   `woltspace-worktui` skill from worktui's own bundled copy, sync the platform
   section of every wolt's `CLAUDE.md`, write trust config / `settings.json` /
   `.bashrc` / git config, assemble the environment every child inherits
   (including `WOLTSPACE_ISOLATION=external` and `WOLTSPACE_ENTRYPOINT=1`), open
   the tmux window the human lands in, start the slack bot if configured, and
   then run the control plane **in-process** via the same `serve` call a native
   user's CLI makes.

Everything else — skills sync, hook normalization, session adoption, the
connectors, the tunnel — belongs to that control plane, which is byte-for-byte
the one a native user runs.

### `server/app.py` (FastAPI)
Python server running on port 7777 inside the container.
- Serves the split view (`/tui?session=X`) — xterm.js terminal + iframe viewport
- Manages per-session viewport URLs (`/current`)
- Serves static files: `public/` (platform UI) → `wolt/site/` → `wolt/sparks/`
- Proxies tool registrations at `/tools`
- Serves apps at `/app/:name/` (static from `dist/` or proxy to port — see `woltspace-apps` skill)
- Live reload via SSE at `/livereload`

### `tui/src/tui-service.js` (Node.js) — the pty bridge
TUI WebSocket service on the API port + 1 (7778 beside the usual 7777; override with `WOLTSPACE_TUI_PORT` or `channels.tui.port`), shipped as the `woltspace-tui-service` bin of `@woltspace/tui`. The only remaining Node service — attaches xterm.js to tmux via `node-pty`. The control plane starts it as a supervised connector (`tui` in `woltspace status` / `GET /health`); nothing launches it by hand.

### `container/bot/core.py` (Python)
The bot brain. Loaded by Telegram/Slack adapters. Uses **litellm** for LLM routing.
- Builds system prompt from wolt memory files
- Defines tools: `claude_code`, `new_session`, `get_tunnel_url`, `check_session`, `get_recent_sessions`, `list_sessions`, `find_session`, `kill_session`, `send_message`, `read_memory`, `list_wolts`, `list_apps`, `generate_image`, `switch_wolt`, `check_update`, `wolf_schedules`, `fire_wolf`
- When `claude_code` is called: spawns a tmux session running `run-session.sh` → Claude Code CLI
- Session metadata (status, routing, creature, viewport) stored in the **session registry** — see `container/lib/sessions.py`

### `container/bot/telegram_adapter.py`
Thin Telegram layer over core. Persists chat history to `.state/chat/{chat_id}.jsonl`. Group chat support (responds when @mentioned).

### `container/skills/`
Discovery files Claude Code reads from `~/.claude/skills/`. Platform skills use `woltspace-` prefix and are synced to all wolts on boot. Current platform skills: `woltspace-start-chat`, `woltspace-create-wolt`, `woltspace-notify`, `woltspace-viewport`, `woltspace-apps`, `woltspace-new-app`, `woltspace-wolf`, `woltspace-update`, `woltspace-session-summary`, `woltspace-worktui` (derived at boot from worktui's own bundled skill — see `derive_worktui_skill` in `src/woltspace/container_entrypoint.py`, not hand-maintained), `woltspace-organize-context`, `woltspace-setup-telegram`, `woltspace-setup-github`.

### `container/cron/digest.mjs`
Daily digest pipeline (3 phases): fetch (HN, HuggingFace, Lobsters) → select via `claude -p` → render HTML. Writes to `wolt/sparks/`. Optional Spotify playlist curation.

### `container/cron/check-update.sh`
No-LLM update checker. Compares stored version (`.state/woltspace-version`) against remote HEAD via `git ls-remote`. Available for on-demand use but not registered as a default wolf cron — wolves are loud, update checking is quiet surveillance.

### `container/creatures/wolf.py` (Wolf Scheduler 🐺)
Background cron service. Reads `wolt/wolf.json` for scheduled tasks, fires them on time, sends 🐺 notifications. Actions: `script` (shell command), `session` (Claude Code session), `skill` (invoke a skill). Tracks last-run per cron entry in `.state/wolf/`. Auto-starts when `wolf.json` exists. See `/wolf` skill for full config format.

CLI: `--list` (show crons), `--once` (fire due crons and exit), `--fire NAME` (trigger a specific cron by name, ignoring schedule — great for debugging).

### `container/creatures/vulture.py` (Vulture Reaper 🦅)
Background session reaper. Cleans up dead sessions — reconciles registry, kills zombie tmux sessions. Platform-level, always on, not wolf-managed.

### `container/lib/sessions.py` (Session Registry)
Centralized session metadata — single source of truth. One JSON file per session in `.state/registry/{session_name}.json`.

Each entry tracks: name, wolt, creature, model, status, timestamps, routing (adapter, chat_id, user_id, thread_ts), viewport URL, session URL.

API: `SessionRegistry.create()`, `.update()`, `.get()`, `.list()`, `.finish()`, `.touch()`, `.reconcile()`, `.delete()`.

Used by core.py, run-session.sh, notify, and server. CLI wrapper: `session-reg`.

### `container/bin/`
Utility scripts available in container PATH:
- `wclaude` — **always use this instead of bare `claude` inside the container.** Trusts `$PWD` via `trust-dir` before launching Claude Code — prevents the workspace trust dialog. All args pass through.
- `trust-dir <directory>` — add a directory to Claude's trusted projects in `~/.claude.json`. Only trusts dirs under `/workspace/wolts/`. Used internally by `wclaude`.
- `push-view <path>` — set viewport URL for current session
- `run-session.sh` — wrapper that injects notification context and runs Claude Code
- `notify` — send message back to originating adapter (Telegram/Slack)
- `session-reg` — CLI for the session registry (`session-reg list`, `session-reg get <name>`, `session-reg reconcile`)
- `create-creature-wolt <name> <type>` — create a new creature-wolt (wolf, dog, rodent, etc.)
- `version-check` — check for newer woltspace release (polls GitHub API, no git fetch)
- `spawn-tool` — register a tool proxy with the server
- `gh-app-token` — print a short-lived GitHub App installation token to stdout (used by `open_issue` tool and available for `gh` CLI auth)

### Worktui (`wt`)
Worktree + Claude session manager, available in all sessions. Manages git worktrees for parallel development — each branch gets an isolated working directory.

Worktrees live at `$WORKTUI_DIR` (`/workspace/wolts/.worktui` by default) — on the mounted volume, so they survive container rebuilds.

**Use worktui when you need to work on a branch in isolation** — especially for platform work (woltspace PRs) or any task where you want a clean checkout without affecting other work.

```bash
# Interactive TUI
wt                                # Browse worktrees, sessions, create/delete

# CLI (non-interactive, scriptable)
wt list [--json]                  # List worktrees
wt create <branch> [--pr]        # Create worktree + optionally draft PR
wt delete <branch> [--branch]    # Delete worktree + optionally its branch
wt sessions [<branch>] [--json]  # List Claude sessions for a worktree
wt projects [--json]             # List registered projects
wt status                        # Current worktree info
wt clean [--dry-run]             # Remove all non-dirty worktrees
wt remote [--json]               # List remote-only branches
wt pr <branch>                   # Show PR URL for branch
```

**Typical workflow:**
1. `wt create nw/fix-bug-x` — creates isolated worktree
2. Work on the fix in the worktree directory
3. Push + open PR
4. `wt delete nw/fix-bug-x --branch` — clean up after merge

Source: `~/worktui/` (cloned from `jerpint/worktui` during image build). Shell wrapper sourced via `wt.sh` in `.bashrc`.

---

## ⚠️ VIEWPORT — HOW TO SHOW THINGS TO THE HUMAN

**The right-hand pane of the split view is the viewport. Use it. Always push what you build.**

The `/viewport` skill explains everything, but the short version:
1. Write your HTML/app to `wolt/site/` (or build an app under `wolts/apps/`)
2. Run `push-view /your-page.html` — this updates the right pane live
3. The human sees it immediately in their browser

`push-view` auto-detects the current session. **If you build something and don't push it to the viewport, the human can't see it.**

Use `/viewport` for full details: URL paths, app serving, live-reload behavior.

---

## Wolt Directory Structure (per wolt)

```
~/.woltspace/wolts/{name}/
  wolt/
    memory/
      identity.md      — personality, values, voice (full load)
      context.md       — current state, open threads (first 80 lines)
      learnings.md     — active patterns (first 40 lines)
      index.md         — memory index for discoverability
      archive/         — grows forever, searched on demand
    apps/              — full-stack apps (each has app.json, served at /app/:name/)
    site/              — static HTML/CSS public space
    sparks/            — generated artifacts
    drafts/
  .state/              — runtime (tunnel URL, session registry)
    registry/          — one JSON file per session (status, routing, metadata)
  CLAUDE.md            — wolt-specific instructions
  wolt.json            — manifest
```

**Isolation boundary:** Sessions run inside the wolt directory. All code edits MUST stay within this directory. The platform (`/workspace/woltspace/`) is read-only to wolts.

---

## Multi-Wolt Setup

Multiple wolts can run in one container. They all share:
- The same server
- The same bot process
- `~/.woltspace/wolts/.state/registry/` for session metadata and notification routing

`~/.woltspace/wolts/woltspace.json` tracks the active wolt per adapter. The bot can `switch_wolt` at runtime.

---

## Communication Channels

Currently supported: **Telegram**, **Slack**. WhatsApp is planned.

Each adapter is a thin Python file over `core.py`. To add a new adapter: copy `telegram_adapter.py`, implement message send/receive, set `BOT_ADAPTER` env var, and give it a `ChannelConnector` in `src/woltspace/channels.py`.

---

## Key Environment Variables

Set in `~/.woltspace/wolts/.env`:

```bash
ENABLE_TELEGRAM_BOT=true
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_USERS=   # comma-separated IDs, or empty for open
ENABLE_SLACK_BOT=false
LLM_MODEL=anthropic/claude-haiku-4-5-20251001  # bot model
WOLTSPACE_PUBLIC_TUNNEL=true  # set false for localhost-only

# GitHub App auth (for issue creation, PRs — run /create-github-bot to set up)
GITHUB_APP_ID=
GITHUB_APP_INSTALLATION_ID=
GITHUB_APP_PRIVATE_KEY=   # PEM key, newlines escaped as \n
```

The container also accepts `WOLT_NAME` as an env var (passed by the CLI during `init` for first boot). After that, the container reads `woltspace.json` to resolve the active wolt.

**Claude auth** is handled during `woltspace init` via the native OAuth flow. Credentials are stored in `~/.woltspace/wolts/.claude/.credentials.json` and persist across container rebuilds.

---

## Testing

Tests live in `test/` and run via `test/run-tests.sh`. The runner sources `/workspace/wolts/.env` for secrets automatically.

```bash
# From /workspace/woltspace:
bash test/run-tests.sh              # all tests (~186 tests, ~2 min)
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
- `test_wolf.py` — wolf scheduler: cron parser, schedule loading, state tracking, dispatch routing, fire-by-name, wolf_schedules/fire_wolf/check_update tools, notify footer logic
- `test_wolts.py` — wolt discovery, creature-type system, creature-wolt creation, singleton demotion, dog identity loading

**CLI smoke tests** (`test/test-cli.sh`) — full cycle: init → start → stop → rebuild → backup → reconcile → idempotent init → shell → version check. Run from host:

```bash
bash test/test-cli.sh              # test main branch
bash test/test-cli.sh --local      # test local code
bash test/test-cli.sh --branch X   # test a specific branch
```

**Environment:**
- `TEST_VERBOSE=1` (default) — posts per-test results to Telegram test group
- `TEST_VERBOSE=0` — summary only
- `TEST_CHAT_ID` — dedicated test group chat (set in `.env`)
- `KEEP_E2E_ARTIFACTS=1` — preserve hello-wolt-test.html and session after e2e test

**Dependencies:** `uv sync --project container/bot` (auto-installed by runner)

---

## Development

### ⚠️ HARD RULE: Never edit on main

**All changes happen on branches, never on main or staging directly.** Use a worktree for every change:

```bash
wt create nw/my-feature      # creates isolated worktree
# ... make changes, commit, push ...
# open PR targeting staging, get it reviewed, merge
wt delete nw/my-feature --branch   # clean up
```

PRs target **main** directly. Use a worktree branch for every change, get it reviewed, merge to main.

**Why:** The image builds from main by default. A bad edit on main breaks every `woltspace rebuild` for everyone. Feature branches are the buffer.

### Local dev workflow

```bash
# Set sticky local dev mode
export WOLTSPACE_LOCAL=true    # add to shell rc

# Build + run from local checkout
woltspace rebuild               # COPY's local code into image
woltspace start                 # shows "⚙ local dev mode"
```

No mounts, no hot-reload. Change code → `woltspace rebuild` → see changes. Same image structure as production.

To test a remote branch without checking it out locally:
```bash
woltspace rebuild --branch staging
```

### PR style — use the lore

PR descriptions should use the colony lore — creatures, places, metaphors. Don't write dry changelogs. Tell the story of what happened and why it matters, in the voice of the colony.

Good examples:
- *"The rebuild was pulling behind the colony's back"* — explains a trust violation in colony terms
- *"The dog cried wolf 🐶🐺"* — false-alarm update checker, the title IS the bug
- *"the dog watches the right stream now"* — one-line closer that lands the fix

Structure: **narrative opener** (what went wrong / what's new, in lore) → **what changed** (concrete, bulleted) → **test plan** → **one-line closer** (optional, creature emoji welcome).

---

## What's Messy / Still Iterating

- **WhatsApp adapter**: not yet implemented
- **Multi-wolt UX**: switching wolts works but UI is minimal
- **Digest cron**: timezone handling is approximate
- The split view UI (`public/split.html`) has grown organically — could use cleanup
- `agents.md` in the repo root is older technical reference, may be out of date
