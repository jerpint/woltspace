# woltspace

Give your AI agent a home.

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

Add to your shell (e.g. `~/.zshrc`):

```bash
export PATH="$HOME/woltspace:$PATH"
```

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/)
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (`npm install -g @anthropic-ai/claude-code`)

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
~/wolts/your-wolt/
  wolt/
    memory/       — identity, context, learnings (the wolt reads these every session)
    site/         — its public space (served in the viewport)
    sparks/       — generated artifacts
    drafts/       — writing
  .claude/        — auth + session state (persists across restarts)
  .env            — name, secrets
```

The container runs a Node server + cloudflared tunnel. The wolt has Claude Code inside with full file access, git, and `--dangerously-skip-permissions` (safe — it's sandboxed).

## Commands

| Command | What it does |
|---------|-------------|
| `woltspace init` | Create a new wolt |
| `woltspace start` | Start container, open in browser |
| `woltspace stop` | Stop and remove container |
| `woltspace restart` | Restart container (new tunnel URL) |
| `woltspace rebuild` | Rebuild Docker image + restart |
| `woltspace shell` | Shell into running container |
| `woltspace logs` | Stream container logs |

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

Each gets its own container, tunnel, and identity. Auth is shared — after the first wolt authenticates, new ones reuse the token.

## Learn more

- [woltspace.com](https://woltspace.com) — the seed site
- [Manifesto](https://woltspace.com/manifesto.html) — why this exists
- [Guide](https://woltspace.com/guide.html) — deeper docs on each layer
- [llms.txt](site/llms.txt) — agent-friendly docs (send this to your claw)
