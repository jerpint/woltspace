# Woltspace Architecture

> A platform for autonomous AI agents ("wolts") that live in Docker containers with persistent identity, memory, and the ability to build real things. Humans interact via Telegram/Slack; the wolt works in Claude Code sessions, pushes output to a split-view browser UI.

## The Three Layers

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
│     13 tools · agent loop · session spawning     │
│     Session registry · memory loading            │
└────────────────────┬────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────┐
│            EXECUTION LAYER                        │
│     Node.js server (port 7777)                   │
│     tmux sessions running Claude Code            │
│     Split-view browser UI · Cloudflare tunnel    │
└─────────────────────────────────────────────────┘
```

---

## Core Files

| File | Lines | What it does |
|------|-------|-------------|
| `server/app.py` | ~800 | FastAPI server (port 7777). All endpoints: TUI, viewport, notify, apps, tools, sessions |
| `container/bot/core.py` | ~1100 | The brain. Haiku agent loop, 13 tools, session spawning, memory loading |
| `container/bot/telegram_adapter.py` | ~490 | Telegram handler: text, voice, photos, den replies, history |
| `container/bot/slack_adapter.py` | ~360 | Slack handler: Socket Mode, thread-based, @mention routing |
| `container/lib/sessions.py` | ~320 | Session registry: one JSON file per session in `.state/registry/` |
| `container/bot/image_gen.py` | ~130 | Image generation wrapper (OpenAI gpt-image-1) |
| `container/bin/run-session.sh` | ~80 | Session wrapper: injects notify context, runs claude CLI, updates registry on exit |
| `container/entrypoint.sh` | ~240 | Container init: skills, hooks, git, tmux, server, tunnel, bots |
| `container/Dockerfile` | ~93 | Image: node:22-slim + claude CLI + python deps + cloudflared |
| `public/split.html` | ~490 | Browser UI: xterm.js terminal (left) + iframe viewport (right) |

---

## How Messages Flow

### Telegram/Slack → Claude Code session

```
User sends "build me a homepage"
  → telegram_adapter.handle_message()
  → core.get_response()                    # Haiku agent loop
  → Haiku picks tool: claude_code
  → core.start_claude_session()
      ├─ registry.create()                 # .state/registry/{name}.json
      ├─ tmux new-session                  # spawns run-session.sh
      └─ returns {name, url, creature}
  → adapter sends ack to Telegram
```

### Claude Code → notification → user reply → back to session

```
Claude in session calls: notify "done, check it out"
  → hooks/notify.sh                        # Claude Code Notification hook
  → POST /notify to server.js
  → server reads registry routing info
  → sends to Telegram/Slack with footer:
      "↩️ reply to this message..."
      "https://tunnel/tui?session=NAME"

User replies in Telegram
  → adapter detects reply-to-notify
  → extracts session name from URL in footer
  → core.message_session(name, text)
      ├─ if Claude running: tmux send-keys
      └─ if exited: revive with claude --continue
```

### Viewport updates (what shows in the browser)

```
Claude Code pushes a view
  → POST /current?session=X {url: "/index.html"}
  → server writes .state/current-url-X.json

split.html polls /current/meta every 2s
  → iframe loads the new URL
  → server injects livereload script into HTML
  → file changes trigger instant reload via /livereload WebSocket
```

---

## server.js Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/` | GET | Home page (session launcher) |
| `/tui?session=X` | GET+WS | Split-view terminal (xterm.js ↔ tmux via node-pty) |
| `/current?session=X` | GET/POST | Viewport URL control per session |
| `/current/meta?session=X` | GET | Viewport metadata + redirect check |
| `/notify` | POST | Send message to originating Telegram/Slack chat |
| `/sessions` | GET | List all sessions from registry |
| `/sessions/new` | POST | Spawn new Claude Code session |
| `/sessions/:id/message` | POST | Send text to session (revives if needed) |
| `/history` | GET | List sparks (artifacts) |
| `/history/:id` | GET | Serve spark HTML with version nav |
| `/app/:name/*` | ALL | Serve wolt apps (static or proxy) |
| `/apps` | GET | List registered apps |
| `/tools` | GET | List running tool proxies |
| `/tools/spawn` | POST | Start a tool process |
| `/tools/:name/*` | ALL+WS | Proxy HTTP/WS to tool port |
| `/shares` | GET/POST/DELETE | Public share link management |
| `/public/:token/*` | GET | No-auth proxy via share token |
| `/memory/read` | POST | Read wolt memory files |
| `/status` | GET | Server + digest status |
| `/views/history` | GET | Recent viewport changes |
| `/livereload` | WS | File-change broadcast for live reload |

---

## core.py Tools (what Haiku can call)

| Tool | Purpose |
|------|---------|
| `claude_code` | Spawn a Claude Code session (beaver=sonnet, raccoon=opus) |
| `new_session` | Spawn + redirect viewport to new session |
| `check_session` | Poll session status + last output |
| `send_message` | Send text to running session (revives if exited) |
| `list_sessions` | All sessions from registry |
| `find_session` | Search sessions by title/prompt |
| `get_recent_sessions` | Read completed sessions from JSONL |
| `kill_session` | Kill a tmux session |
| `get_tunnel_url` | Get current public URL |
| `read_memory` | Read memory files (scoped to wolt/memory/) |
| `list_wolts` | Multi-wolt: show available wolts |
| `list_projects` | List projects in current wolt (names, paths, metadata) |
| `switch_wolt` | Change active wolt identity |
| `check_update` | Check if woltspace update is available (git ls-remote, no LLM) |
| `generate_image` | AI image gen (OpenAI) |

---

## Session Lifecycle

```
CREATED → RUNNING → COMPLETED / FAILED
                  ↘ ORPHANED (tmux died without exit handler)
```

- **Registry**: one JSON file per session in `.state/registry/{name}.json`
- **Naming**: `{wolt}-{adj}-{noun}-{6hex}` (e.g. `neowolt-chompy-dam-a3f1e2`)
- **Creature system**: beaver (🦫 sonnet) or raccoon (🦝 opus) — controls which Claude model runs
- **Reconciliation**: `registry.reconcile()` checks tmux, marks dead sessions orphaned
- **No cleanup**: orphaned registry files accumulate (manual cleanup needed)

---

## Container Startup (entrypoint.sh)

```
1. Install dev deps (if volume-mounted)
2. Resolve WOLT_DIR from WOLTS_DIR/WOLT_NAME
3. Copy skills: platform defaults + wolt overrides → ~/.claude/skills/
4. Seed default wolf.json if active wolt doesn't have one (update checker)
5. SSH/git config (deploy key if present)
6. Write OAuth credentials
7. Install Claude Code hooks (notify + session-done)
8. Start tmux main session → auto-launch Claude
9. Start TUI pty service (port 3001)
10. Start FastAPI server (port 7777)
11. Start cloudflared tunnel → write URL to .state/tunnel-url
12. Start Telegram bot (optional, watchfiles reload)
13. Start Slack bot (optional, watchfiles reload)
14. Start wolf scheduler (if wolf.json exists)
15. Symlink node_modules for ESM resolution
16. wait -n (exit if ANY critical process dies)
```

---

## File Layout (per wolt)

```
~/wolts/{name}/
├─ wolt/
│  ├─ memory/           # identity, context, learnings (boot files)
│  │  └─ archive/       # grows forever, searched on demand
│  ├─ projects/         # isolated code projects (apps, scripts, experiments)
│  ├─ site/             # static HTML/CSS, live-reload watched
│  ├─ apps/             # full-stack apps (each has app.json)
│  ├─ sparks/           # generated artifacts (digest, etc.)
│  ├─ drafts/           # manifesto, etc.
│  └─ images/           # AI-generated images
├─ .claude/
│  ├─ skills/           # wolt-specific skill overrides
│  ├─ settings.json     # hooks config
│  └─ .credentials.json # OAuth token
├─ .state/
│  ├─ registry/         # session JSON files
│  ├─ tunnel-url        # current public URL
│  ├─ chat/             # Telegram/Slack message history (JSONL)
│  └─ bot-debug/        # bot.jsonl event log
├─ .env                 # secrets (gitignored)
├─ CLAUDE.md            # wolt-specific instructions
└─ wolt.json            # manifest
```

---

## Known Issues & Future Optimization Notes

_Use this section to jot down ideas as you work with the codebase._

### Complexity hotspots
- **server.js (~1400 lines)** — single file doing HTTP, WebSocket, file watching, notifications, session management, tool proxy, share links. Could be split into modules (routes, ws, notify, proxy).
- **core.py (~1100 lines)** — agent loop + all 13 tool implementations in one file. Tool functions could be extracted.

### Architecture concerns
- **Session lifecycle is implicit** — no state machine, relies on tmux existence checks. Reconciliation is reactive, not proactive.
- **No orphan cleanup** — dead registry files accumulate in `.state/registry/`.
- **Notify is split across languages** — Telegram notify goes through Node (server.js POST /notify → Telegram API), while bot responses go through Python (telegram_adapter.py). Two codepaths owning Telegram.
- **`_send_ack` only handles Telegram** — Slack users get no "🦫 on it" ack when sessions spawn.
- **History window is fixed** — `MAX_HISTORY = 20` pairs for Telegram, Slack pulls full thread from API (inconsistent).

### Things that work well
- **Session registry** — clean single-source-of-truth pattern, one JSON file per session.
- **Den reply routing** — elegant: footer embeds session URL, adapter extracts it, message goes directly to tmux.
- **Live reload** — file watcher + WebSocket broadcast + injected script = instant updates.
- **Creature system** — simple model selection via metaphor (beaver/raccoon).
- **Skills** — platform defaults baked in, wolt overrides win. Clean layering.

### Potential optimizations
- [ ] Split server.js into modules (routes/, ws/, middleware/)
- [ ] Extract tool implementations from core.py into separate files
- [ ] Add session TTL / auto-cleanup for orphaned registry entries
- [ ] Consolidate Telegram into Python (eliminate Node→Telegram path)
- [ ] Add Slack ack messages for session spawns
- [ ] Consider event-driven viewport updates (SSE/WS) instead of 2s polling
- [ ] Session state machine with explicit transitions

---

*Generated 2026-03-13. This is a living document — add notes as you go.*
