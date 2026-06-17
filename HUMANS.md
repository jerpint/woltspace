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
| `woltspace backup [tag]` | Snapshot container + wolts (tag defaults to datetime) |
| `woltspace backup [tag] --bundle` | Same, but zipped into one portable file |
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
      apps/              — apps
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

## Multi-user permissions (optional)

By default a woltspace container is single-tenant — anyone past the tunnel
sees and controls every wolt. To gate per user (collaborators, family,
small group), there's an opt-in mode that uses your existing Cloudflare
Access setup.

### Setup

1. **Seed yourself** into `wolts/.space/auth/users.json` so you don't
   lock yourself out. From inside the container (or via a wolt session
   using the `woltspace-access` skill):
   ```bash
   access add you@example.com '*'
   ```
   The `'*'` is a wildcard meaning "every wolt."

2. **Enable auth** in `~/.woltspace/wolts/.env`:
   ```bash
   WOLTSPACE_AUTH=cloudflare
   WOLTSPACE_CF_TEAM_DOMAIN=yourteam.cloudflareaccess.com
   WOLTSPACE_CF_AUD=<application audience tag from CF Zero Trust>
   ```

3. **Restart** the server. Visit the lodge — Cloudflare Access asks for
   email OTP, the JWT lands at the server, the middleware validates it
   and looks up your email in `users.json`.

### Adding collaborators

Two steps:

1. Add their email to the Cloudflare Access policy
   (Zero Trust → Access → Applications → policies → emails) so they
   can reach the tunnel.

2. Add them to `users.json`:
   ```bash
   access add bob@example.com bloggo shared-wolt
   ```

`users.json` looks like:

```json
{
  "users": [
    {"email": "you@example.com",          "wolts": ["*"]},
    {"email": "collaborator@example.com", "wolts": ["bloggo"]}
  ]
}
```

Users see and control only wolts in their allow-list. Apps inherit
access from their keeper wolt. A user can also create new wolts via the
lodge UI — when they do, the new wolt is auto-appended to their own
allow-list (self-onboarding).

### Scope of enforcement

This is application-layer. The lodge UI shows only what the user is
allowed to see, and the REST API refuses cross-wolt requests. It does
**not** stop a session that's already running from reading another
wolt's files via the shell — all sessions still run as the same OS
user. That's tracked separately (filesystem isolation, issue #354).

Default mode is `WOLTSPACE_AUTH=none` — single-tenant, today's behavior,
zero configuration. Skip this whole section if that's what you want.

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

## GitHub integration

Wolts can open issues and PRs on GitHub using a **GitHub App** (short-lived tokens, no long-lived PATs).

To set it up, ask your wolt to run `/create-github-bot` — it walks you through creating a GitHub App, generating a private key, and adding the credentials to `.env`:

```bash
GITHUB_APP_ID=<app id>
GITHUB_APP_INSTALLATION_ID=<installation id>
GITHUB_APP_PRIVATE_KEY=<PEM key with newlines escaped as \n>
```

Once configured, the bot's `open_issue` tool works automatically. You can also use the token directly:

```bash
GH_TOKEN=$(gh-app-token) gh issue list --repo jerpint/woltspace
```

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

## Backup & recovery

Before any risky change (migration, major update, experiment), snapshot everything:

```bash
woltspace backup                    # auto-tags with datetime
woltspace backup pre-migration      # custom tag
```

This creates a **matched pair** — same tag on both:
- **Container image:** `woltspace-backup:<tag>` (runtime, deps, installed tools)
- **Wolts copy:** `~/.woltspace/wolts-backup-<tag>` (all your data)

To restore:

```bash
docker stop woltspace && docker rm woltspace
docker run -d --name woltspace \
  -v ~/.woltspace/wolts-backup-<tag>:/workspace/wolts:rw \
  -p 7777:7777 \
  woltspace-backup:<tag>
```

Container + data from the same moment, matched by name.

### Portable bundle

For a single-file backup you can take anywhere:

```bash
woltspace backup pre-migration --bundle
```

This creates `~/.woltspace/woltspace-backup-pre-migration.zip` containing the Docker image, wolts, and a restore script. To restore on any machine with Docker:

```bash
unzip woltspace-backup-pre-migration.zip -d restore
cd restore && bash restore.sh
```

Note: bundles are large (2-3GB) since they include the full Docker image.

### Cleanup

```bash
docker rmi woltspace-backup:<tag>
rm -rf ~/.woltspace/wolts-backup-<tag>
rm -f ~/.woltspace/woltspace-backup-<tag>.zip
```

## Version checking

The container stamps a version in `.version` at build time (git tag if available, otherwise commit hash).

From inside the container:
```bash
version-check              # prints current vs latest, exit 0 if up to date
version-check --quiet      # exit code only (0 = current, 1 = update available)
```

This polls the GitHub releases API — no git fetch, no local changes. Safe for cron or on-demand checks.

## Learn more

- [woltspace.com](https://woltspace.com) — the seed site
- [Manifesto](https://woltspace.com/manifesto.html) — why this exists
- [Guide](https://woltspace.com/guide.html) — deeper docs on each layer
