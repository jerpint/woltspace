# 🦫 woltspace

give your wolt space to build.

## What is this?

`woltspace` is a CLI that creates and runs **wolts** — AI agents that live inside Docker containers with their own identity, memory, and a space they build with you.

Each wolt gets:
- A **split view** in the browser (terminal left, viewport right)
- **Persistent memory** across sessions
- A **space** (website) it builds and maintains
- Full autonomy inside a sandboxed container

## Install

```bash
git clone https://github.com/jerpint/woltspace
```

Then run `woltspace init` — it will offer to add itself to your PATH automatically.

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/)

## Quick start

```bash
woltspace init
```

That's it. You'll be asked for a name, then:

1. A Docker container builds with everything baked in
2. A tunnel URL appears — open it in your browser
3. Your wolt wakes up and introduces itself
4. First time: Claude asks you to authenticate (one-time, click the link)
5. The onboarding conversation starts — your wolt figures out who it is, with your help

After that:

```bash
cd ~/wolts/your-wolt-name
woltspace start
```

## What happens inside

```
~/wolts/
  .env                   — shared secrets for all wolts (api keys, bot config)
  woltspace.json         — which wolt is active
  your-wolt/
    wolt/
      memory/            — identity, context, learnings (read every session)
      site/              — public space (served in the viewport)
      sparks/            — generated artifacts
      drafts/            — writing
    .claude/             — auth + session state (persists across restarts)
    .state/              — tunnel URL, session routing
```

The container runs a Node server + cloudflared tunnel. The wolt has Claude Code inside with full file access, git, and `--dangerously-skip-permissions` (safe — it's sandboxed).

## Commands

| Command | What it does |
|---------|-------------|
| `woltspace init` | Create a new wolt |
| `woltspace start` | Start container, open in browser |
| `woltspace stop` | Stop and remove container |
| `woltspace restart` | Restart container (new tunnel URL) |
| `woltspace rebuild` | Rebuild image from `main` (stable) + restart |
| `woltspace rebuild --dev` | Rebuild image from `staging` (latest, may be rough) |
| `woltspace update` | Check if a new version is available |
| `woltspace shell` | Shell into running container |
| `woltspace logs` | Stream container logs |

## Updates

Updates are opt-in — your wolt checks for them daily and lets you know.

**How it works:**
- A wolf cron runs once a day, checks if `main` has moved ahead of your local version
- If an update is available, you get a 🐺 notification: *"update available — ask a beaver or raccoon"*
- You can also ask the dog directly: *"is there an update?"*
- When ready, ask any rodent session to update — it'll evaluate the changes, explain impact, and wait for your go-ahead
- Or from the host: `woltspace update` to check manually

**Two branches:**
- `woltspace rebuild` builds from `main` — stable, tested, what you should run
- `woltspace rebuild --dev` builds from `staging` — latest development, may have rough edges

## How it works

```
┌─────────────────────────────────────────────────┐
│  browser (tunnel URL)                           │
│  ┌──────────────────┐  ┌─────────────────────┐  │
│  │    terminal       │  │     viewport        │  │
│  │    (tmux/claude)  │  │  (wolt's space)     │  │
│  │                   │  │                     │  │
│  └──────────────────┘  └─────────────────────┘  │
└─────────────────────────────────────────────────┘
        │                         │
        ▼                         ▼
┌─ docker container ──────────────────────────────┐
│  claude code    node server    cloudflared       │
│       │              │              │            │
│       ▼              ▼              ▼            │
│  ~/wolts/name/wolt/  (mounted rw from host)     │
└─────────────────────────────────────────────────┘
```

- The **terminal** is a tmux session accessed via xterm.js in the browser
- The **viewport** shows whatever the wolt pushes to it (`POST /current`)
- The wolt repo is **mounted** from the host — files persist across container rebuilds
- The tunnel URL is **ephemeral** — changes on restart, no account needed

## Multiple wolts

All wolts live under `~/wolts/`. Create as many as you want:

```bash
woltspace init    # creates ~/wolts/alice
woltspace init    # creates ~/wolts/bob
```

All wolts share one container — `~/wolts/` is mounted in full. `woltspace.json` tracks which wolt is active (boots in the main tmux session). Auth is shared — after the first wolt authenticates, new ones reuse the token.

## Messaging (Telegram, etc.)

Wolts can talk through messaging apps. The bot code is baked into the image — just add config.

```bash
# In ~/wolts/.env:
ENABLE_TELEGRAM_BOT=true
TELEGRAM_BOT_TOKEN=<from @BotFather>
TELEGRAM_ALLOWED_USERS=<your telegram user id>
ANTHROPIC_API_KEY=<key>              # or OPENROUTER_API_KEY= for other providers
LLM_MODEL=anthropic/claude-haiku-4-5-20251001
```

Then `woltspace restart`. The bot starts automatically.

**How it works:** A small fast model (Haiku, or any provider via litellm) handles casual conversation using the wolt's memory. When a task comes in, it spawns a Claude Code session and sends back a link to the TUI — tap it on your phone and you're in a live terminal.

**Commands:** `/sessions` (list active sessions with links), `/kill <name>` (cleanup)

**Customizing:** Copy `/workspace/woltspace/container/bot/` to `wolt/bot/` in your repo and edit freely. The entrypoint prefers your code over the platform default.

## Named sessions

Each task gets its own tmux session, accessible via the browser:

```
https://<tunnel>/tui?session=task-12345
```

The main session is always at `/tui` (or `/tui?session=main`). `GET /sessions` returns a JSON list of all active sessions.

## Stack

```
Container image (~800MB):
├── node:22-slim          — base (server is JS)
├── server.js             — HTTP + WebSocket + split view (single file, ~900 lines)
├── tmux                  — session multiplexer (named sessions, survives disconnects)
├── xterm.js (CDN)        — terminal in browser
├── ws + node-pty         — WebSocket + PTY for terminal
├── cloudflared           — tunnel to internet (no account, ephemeral URL)
├── claude                — native binary (installed via curl|bash, auto-updates)
├── uv                    — Python package manager (runs bot)
├── /workspace/woltspace/container/bot/             — Telegram bot (litellm + python-telegram-bot)
├── /workspace/woltspace/container/skills/          — platform skills (create-wolt, telegram, digest, music, work)
├── /workspace/woltspace/container/cron/            — digest pipeline
└── /workspace/woltspace/public/          — split view UI assets

Host mount (persists across rebuilds):
└── ~/wolts/
    ├── .env              — shared secrets + feature flags (api keys, bot config)
    ├── woltspace.json    — active wolt config
    └── <name>/
        ├── wolt/memory/  — identity, context, learnings
        ├── wolt/site/    — public space
        ├── wolt/sparks/  — generated artifacts
        ├── wolt/bot/     — bot override (optional)
        ├── .claude/      — auth + session state
        └── .state/       — tunnel-url, session routing
```

**Design choices:**
- Two runtimes (Node + Python) — server is JS for xterm.js/WebSocket ecosystem, bot is Python for litellm's provider coverage
- Single server.js — monolith by choice, not accident. One file to read, one process to manage
- make + g++ in image — only for node-pty native compilation. Future: prebuilt binaries or multi-stage build
- Claude CLI in isolated build stage — cached across rebuilds, not re-downloaded on source changes. To update Claude: `docker build --no-cache-filter=claude -t woltspace -f container/Dockerfile .`
- Feature flags in .env — `ENABLE_TELEGRAM_BOT`. Telegram on by default (skipped silently if token missing). Digest scheduling is handled by the wolf creature, not server.js

## Learn more

- [woltspace.com](https://woltspace.com) — the seed site
- [Manifesto](https://woltspace.com/manifesto.html) — why this exists
- [Guide](https://woltspace.com/guide.html) — deeper docs on each layer
- [llms.txt](site/llms.txt) — agent-friendly docs (send this to your claw)
