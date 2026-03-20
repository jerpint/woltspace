# woltspace — agent reference

Technical reference for agents working on or with the woltspace codebase.

## Repo structure

```
woltspace/
  server.js               — Node server (756 lines)
  woltspace               — CLI bash script (309 lines)
  container/
    Dockerfile            — node:22-slim base image
    entrypoint.sh         — container startup (128 lines)
    bot/
      core.py             — bot brain: memory, tools, LLM routing (204 lines)
      telegram_adapter.py — telegram-specific handler (177 lines)
      pyproject.toml      — python deps (litellm, python-telegram-bot)
    cron/
      digest.mjs          — daily digest pipeline (716 lines)
    skills/               — platform skills (copied to ~/.claude/skills/)
      create-wolt/        — first-run onboarding
      digest/             — curated daily digest
      music/              — spotify playlist curation
      viewport/           — push content to split view
      telegram/           — telegram bot setup guide
  public/
    split.html            — split view UI (xterm.js + iframe)
  site/                   — woltspace.com content
  template/               — template for new wolts (copied by `woltspace init`)
```

## How the container works

**Image:** `node:22-slim` with cloudflared, uv, tmux, Claude Code CLI baked in.

**Mounts at runtime** (set up by `woltspace` CLI):
- `$wolt_dir:/workspace/wolt:rw` — the wolt's repo (identity, site, sparks, memory)
- `$wolt_dir/.claude:/home/node/.claude:rw` — auth and session state
- Optional: deploy key at `/home/node/.ssh/deploy-key:ro`

**Entrypoint sequence** (`container/entrypoint.sh`):
1. Copy platform skills to `~/.claude/skills/`, then wolt overrides on top
2. Set up SSH config for deploy key (if present)
3. Write `.claude.json` to skip onboarding and trust the workspace
5. Configure git user as `$WOLT_NAME`
6. Create tmux `main` session, auto-start Claude Code in it
7. Start Node server with `--watch` (auto-restarts on file changes)
8. Start cloudflared tunnel, write URL to `.state/tunnel-url`
9. Start Telegram bot if `ENABLE_TELEGRAM_BOT=true` (backgrounded, disowned)
10. `wait -n` on server + tunnel — container dies if either exits

**Key env vars:**
- `WOLT_DIR` — mount point for wolt repo (default: `/workspace/wolt`)
- `WOLT_NAME` — wolt's name
- `ENABLE_TELEGRAM_BOT`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USERS`
- `SPOTIFY_*` — Spotify API credentials for music curation
- `OPENROUTER_API_KEY`, `LLM_MODEL` — LLM provider for bot chat

## Server (server.js)

Single Node.js HTTP server on port 7777. No framework. Serves everything.

**Routes:**

| Method | Path | What it does |
|--------|------|-------------|
| GET | `/` or `/tui` | Split view (split.html) — terminal left, viewport right |
| GET | `/tui?session=X` | Split view for a named tmux session |
| POST | `/current` | Set viewport URL: `{"url": "/path"}`, optional `?session=X` |
| GET | `/current` | Get current viewport URL |
| GET | `/current/meta` | Get current URL + title |
| GET | `/status` | System status JSON (digest state, etc.) |
| GET | `/sessions` | List active tmux sessions |
| GET | `/history` | List all sparks as JSON |
| GET | `/history/:id` | Serve a spark's HTML |
| GET | `/views/history` | View navigation history (JSONL) |
| POST | `/tools/spawn` | Register a tool proxy |
| GET | `/tools` | List registered tool proxies |
| GET | `/tools/:name/*` | Proxy to a registered tool |
| GET | `/*` | Static files from `wolt/site/` (wolt's space) |

**WebSocket:** `/tui` upgrades to WS for xterm.js ↔ node-pty ↔ tmux.

**Static file serving:** `wolt/site/` is served at root. `public/` (platform UI like split.html) takes priority. Sparks served from `wolt/sparks/`.

**Digest scheduling:** Owned by the wolf creature (see `creatures/wolf.py`). The active wolf-wolt's `wolt/wolf.json` defines the cron schedule. Wolf runs scripts in named tmux sessions with completion notifications.

**LiveReload:** Watches `wolt/site/` for changes, notifies connected split views via SSE at `/livereload`.

## Bot architecture

Two layers:

**`core.py`** — the brain. Provider-agnostic, adapter-agnostic.
- Loads memory (identity full, context trimmed to 80 lines, learnings to 40)
- Builds system prompt with personality + memory
- Routes through litellm with tool definitions
- Tools: `claude_code` (spawn Claude session in tmux), `get_tunnel_url` (read from .state)
- Returns `{"type": "text", "text": "..."}` or `{"type": "session", "session": {...}}`

**`telegram_adapter.py`** — thin Telegram layer.
- Persists chat history to `.state/chat/{chat_id}.jsonl` (survives restarts)
- Loads last 20 message pairs into LLM context
- Includes reply-to context when replying to messages
- Handlers: messages, /start, /sessions, /kill

**Adding a new adapter** (e.g. Slack):
1. Create `slack_adapter.py` next to `telegram_adapter.py`
2. Import and call `get_response(message, history)` from `core.py`
3. Handle the return type (text → send message, session → send link)
4. Add startup logic in `entrypoint.sh` (same pattern as Telegram)

**Adding a new tool:**
1. Add to `TOOLS` list in `core.py`
2. Add handler in `_handle_tool_call()`
3. Tools that return data (not sessions) get a follow-up LLM call to produce a natural response

**Restarting the bot after code changes:**
```bash
pkill -f telegram_adapter
cd /app && uv run --project bot/pyproject.toml python -m bot.telegram_adapter &
```
The server auto-restarts on file changes via `node --watch`. The bot does not — kill and relaunch manually.

## Digest pipeline (digest.mjs)

Three phases:
1. **Fetch** (~5s) — Parallel JS fetches from HN, HF papers, Lobsters. Fetches OG images.
2. **Select** (~10s) — `claude -p` (one turn, no tools) picks items + writes a reflection. Returns JSON indices into pre-fetched data.
3. **Render** (instant) — JS renders HTML template from selected items. Writes spark to `wolt/sparks/`.

Music: If Spotify credentials are set, uses a separate Sonnet call with web search to build a themed playlist. Searches Spotify API to verify tracks exist. Creates a real Spotify playlist.

All paths use `WOLT_DIR` env var. Memory is read for curation context. Sparks are written to `$WOLT_DIR/wolt/sparks/`.

## Skills system

Skills are `SKILL.md` files in `container/skills/<name>/`. Claude Code auto-discovers them from `~/.claude/skills/`.

**Load order** (entrypoint):
1. Platform skills from `/workspace/woltspace/skills/` (baked into image)
2. Wolt-specific overrides from `$WOLT_DIR/.claude/skills/` (mounted)
3. Wolt overrides win on conflict (same directory name)

**Creating a skill:**
```
container/skills/my-skill/SKILL.md
```

With YAML frontmatter:
```yaml
---
name: my-skill
description: What it does.
---
```

The body is markdown instructions that Claude follows when the skill is invoked.

## CLI (woltspace)

Bash script. Key design:
- `WOLTSPACE_DIR` = where the script lives (the woltspace repo)
- `WOLT_DIR` = cwd when the command is run (the wolt's repo)
- Container name: `woltspace-$WOLT_NAME`
- `init` creates from template, all other commands operate on existing wolt
- `stop --all` kills all `woltspace-*` containers
- Reads `.env` for config, mounts wolt repo + .claude dir + optional deploy key

## File paths inside the container

```
/workspace/woltspace/                    — platform code (baked in)
  server.js
  bot/
  cron/
  skills/
  public/
  entrypoint.sh
/workspace/wolt/         — wolt repo (mounted)
  wolt/memory/
  wolt/site/
  wolt/sparks/
  .env
  .state/
  .claude/
/home/node/.claude/      — claude config (mounted from wolt's .claude/)
  skills/                — merged skills (platform + wolt overrides)
  .credentials.json      — OAuth token
```
