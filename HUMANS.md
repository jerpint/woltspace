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
curl -fsSL https://woltspace.com/install.sh | bash
```

Or clone the repo:

```bash
git clone https://github.com/jerpint/woltspace
```

Then run `woltspace init` — it will offer to add itself to your PATH automatically.

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/)

That's it. No git, no Python, no Node required on your machine.

## Quick start

```bash
woltspace init
```

That's it. You'll be asked for a name, then:

1. A Docker image builds with everything baked in
2. Your wolt is live at `http://localhost:7777`
3. If you enabled the public link, a tunnel URL appears too
4. First time: Claude asks you to authenticate (one-time, click the link)
5. The onboarding conversation starts — your wolt figures out who it is, with your help

After that:

```bash
woltspace start
```

## Commands

| Command | What it does |
|---------|-------------|
| `woltspace init` | Create a new wolt (or reconnect existing ones) |
| `woltspace start` | Start, restart, or resume container |
| `woltspace stop` | Stop and remove container |
| `woltspace rebuild` | Rebuild image from `main` + restart |
| `woltspace shell` | Shell into running container |
| `woltspace chat` | Open Claude directly in container |
| `woltspace logs` | Stream container logs |

### Flags

| Flag | What it does |
|------|-------------|
| `--local` | Build image from local repo instead of git clone |
| `--branch <name>` | Build image from a specific branch (default: main) |

## Where things live

All your wolt data lives in `~/.woltspace/wolts/` (or `$WOLTS_DIR` if you set it):

```
~/.woltspace/wolts/
  .env                   — shared secrets for all wolts
  woltspace.json         — which wolt is active
  your-wolt/
    wolt/
      memory/            — identity, context, learnings
      site/              — public space (served in the viewport)
      sparks/            — generated artifacts
      projects/          — code projects
    .state/              — tunnel URL, session registry
```

This is your backup. `~/.woltspace/wolts/` is the **entire app state** — the container is disposable.

## Public tunnel

During `woltspace init`, you'll be asked:

```
enable public link? [Y/n]
```

Saying **yes** creates a [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/) — an ephemeral public URL so you can access your wolt from anywhere (phone, another machine, etc.). No account needed, URL changes on every restart.

Saying **no** means `http://localhost:7777` only. You can change this anytime in `~/.woltspace/wolts/.env`:

```bash
WOLTSPACE_PUBLIC_TUNNEL=true   # or false
```

Then `woltspace stop && woltspace start` to apply.

## Updates

Updates happen inside the container. Ask your wolt:

> "can you update woltspace?"

Or rebuild from the host:

```bash
woltspace rebuild                     # latest main
woltspace rebuild --branch staging    # latest staging (may be rough)
```

## How it works

```
┌─────────────────────────────────────────────────┐
│  browser (localhost:7777 or tunnel URL)          │
│  ┌──────────────────┐  ┌─────────────────────┐  │
│  │    terminal       │  │     viewport        │  │
│  │    (tmux/claude)  │  │  (wolt's space)     │  │
│  └──────────────────┘  └─────────────────────┘  │
└─────────────────────────────────────────────────┘
        │                         │
        ▼                         ▼
┌─ docker container ──────────────────────────────┐
│  claude code    server    cloudflared (optional) │
│       │           │              │               │
│       ▼           ▼              ▼               │
│  ~/.woltspace/wolts/  (mounted rw from host)     │
└─────────────────────────────────────────────────┘
```

- The **terminal** is a tmux session accessed via xterm.js in the browser
- The **viewport** shows whatever the wolt pushes to it
- `~/.woltspace/wolts/` is **mounted** — files persist across container rebuilds
- The tunnel URL is **ephemeral** — changes on restart, no account needed
- The container image has the woltspace repo baked in via `git clone`

## Multiple wolts

All wolts live under `~/.woltspace/wolts/`. Create as many as you want:

```bash
woltspace init    # creates alice
woltspace init    # creates bob
```

All wolts share one container. `woltspace.json` tracks which wolt is active. Auth is shared — after the first wolt authenticates, new ones reuse the token.

## Messaging (Telegram, etc.)

Wolts can talk through messaging apps. Add config to `~/.woltspace/wolts/.env`:

```bash
ENABLE_TELEGRAM_BOT=true
TELEGRAM_BOT_TOKEN=<from @BotFather>
TELEGRAM_ALLOWED_USERS=<your telegram user id>
ANTHROPIC_API_KEY=<key>
LLM_MODEL=anthropic/claude-haiku-4-5-20251001
```

Then `woltspace stop && woltspace start`. The bot starts automatically.

## Local development

For contributors working on woltspace itself:

```bash
# One-time: set sticky local dev mode
export WOLTSPACE_LOCAL=true    # add to your shell rc

# Build + run from your local checkout
woltspace rebuild               # builds image from local repo
woltspace start                 # shows "⚙ local dev mode"

# Or per-command
woltspace rebuild --local       # same thing, explicit
woltspace rebuild --branch staging  # build from a remote branch
```

**How `--local` works:** Instead of `git clone` inside the Docker image, the local repo is `COPY`'d in. The image is fully self-contained — no mounts, no hot-reload. Change code → `woltspace rebuild` → see changes.

**How `--branch` works:** The Docker image does `git clone --branch <name>` from GitHub. No local repo needed at all.

Both produce the same image structure. The only mount is `~/.woltspace/wolts/`.

## Learn more

- [woltspace.com](https://woltspace.com) — the seed site
- [Manifesto](https://woltspace.com/manifesto.html) — why this exists
- [Guide](https://woltspace.com/guide.html) — deeper docs on each layer
