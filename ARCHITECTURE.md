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
│     FastAPI server (port 7777)                   │
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
| `container/lib/sessions.py` | ~400 | Session registry: one JSON file per wolt at `wolts/{wolt}/.state/sessions/` |
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
      ├─ registry.create()                 # wolts/{wolt}/.state/sessions/{name}.json
      ├─ tmux new-session                  # spawns run-session.sh
      └─ returns {name, url, creature}
  → adapter sends ack to Telegram
```

### Claude Code → notification → user reply → back to session

```
Claude in session calls: notify "done, check it out"
  → hooks/notify.sh                        # Claude Code Notification hook
  → POST /notify to FastAPI
  → server reads session routing info
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
  → server writes viewport_url into session JSON

split.html polls /current/meta every 2s
  → iframe loads the new URL
  → server injects livereload script into HTML
  → file changes trigger instant reload via /livereload WebSocket
```

---

## FastAPI Endpoints (server/app.py)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/` | GET | Home page — the lodge (public/home.html) |
| `/tui` | GET+WS | Split-view terminal (xterm.js ↔ tmux via tui-service.js) |
| `/onboard` | GET | Auth wizard (public/onboard.html) |
| `/current?session=X` | GET/POST | Viewport URL control per session |
| `/current/meta?session=X` | GET | Viewport metadata + redirect (reads session JSON) |
| `/notify` | POST | Send message to originating Telegram/Slack chat |
| `/sessions` | GET | List all sessions (scans per-wolt `.state/sessions/`) |
| `/sessions/new/{adapter}` | POST | Spawn session — lodge, telegram, slack, or create |
| `/sessions/redirect` | POST | Set redirect on a session |
| `/sessions/{id}/message` | POST | Send text to session (revives if needed) |
| `/wolts` | GET | List all wolts from wolt.json files |
| `/apps` | GET | List apps (from `wolts/apps/`) |
| `/apps/{name}/start` | POST | Start an app |
| `/apps/{name}/stop` | POST | Stop an app |
| `/app/{name}/*` | ALL | Serve app (proxy or static) |
| `/wolt/{name}/site/*` | ALL | Serve wolt sites (per-wolt livereload proxy) |
| `/history` | GET | List sparks (artifacts) |
| `/history/{id}` | GET | Serve spark HTML with version nav |
| `/tools` | GET | List running tool proxies |
| `/tools/spawn` | POST | Start a tool process |
| `/tools/{name}/*` | ALL+WS | Proxy HTTP/WS to tool port |
| `/shares` | GET/POST/DELETE | Public share link management |
| `/public/{token}/*` | GET | No-auth proxy via share token |
| `/memory/read` | POST | Read wolt memory files |
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
| `list_apps` | List apps in current wolt (names, paths, metadata) |
| `switch_wolt` | Change active wolt identity |
| `check_update` | Check if woltspace update is available (git ls-remote, no LLM) |
| `generate_image` | AI image gen (OpenAI) |

---

## Session Lifecycle

```
CREATED → RUNNING → COMPLETED / FAILED
                  ↘ ORPHANED (tmux died without exit handler)
```

- **Registry**: one JSON file per session at `wolts/{wolt}/.state/sessions/{name}.json`
- **Viewport URL**: stored in session JSON (`viewport_url` field) — no separate files
- **Redirects**: stored in session JSON (`redirect_to` field) — atomically cleared on read
- **Multi-adapter routing**: `routing` is an array — start on Slack, pick up in browser, get notified on Telegram
- **Naming**: `{wolt}-{adj}-{noun}-{6hex}` (e.g. `neowolt-chompy-dam-a3f1e2`)
- **Creature system**: otter (🦦 haiku), beaver (🦫 sonnet), or raccoon (🦝 opus) — controls which Claude model runs
- **Reconciliation**: `registry.reconcile()` checks tmux, marks dead sessions orphaned
- **Cleanup**: vulture reaps dead tmux sessions (state at `wolts/.space/vulture/`)

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
11. Start cloudflared tunnel → write URL to .space/platform/tunnel-url
12. Start Telegram bot (optional, watchfiles reload)
13. Start Slack bot (optional, watchfiles reload)
14. Start wolf scheduler (if wolf.json exists)
15. Symlink node_modules for ESM resolution
16. wait -n (exit if ANY critical process dies)
```

---

## File Layout

### Per wolt (`wolts/{name}/`)

```
wolts/{name}/
├─ wolt/
│  ├─ memory/           # identity, context, learnings (boot files)
│  │  └─ archive/       # grows forever, searched on demand
│  ├─ site/             # static HTML/CSS, per-wolt livereload
│  ├─ sparks/           # generated artifacts (digest, etc.)
│  ├─ drafts/           # writing and drafts
│  └─ images/           # AI-generated images
├─ .claude/
│  ├─ skills/           # wolt-specific skill overrides
│  ├─ settings.json     # hooks config
│  └─ .credentials.json # OAuth token
├─ .state/
│  ├─ sessions/         # one JSON per session (viewport_url, routing, status)
│  ├─ site.json         # livereload port, pid, dir
│  ├─ wolf/             # cron execution state
│  └─ sessions.jsonl    # append-only session log
├─ .env                 # secrets (gitignored)
├─ CLAUDE.md            # wolt-specific instructions
└─ wolt.json            # manifest (name, type, role, description)
```

### Global (`wolts/.space/`)

```
wolts/.space/
├─ platform/            # tunnel-url, woltspace-version, branch
├─ apps/                # running app state (port, pid)
├─ wolf/                # wolf scheduler state
├─ vulture/             # session reaper state
└─ logs/                # bot.jsonl event log
```

### Apps (`wolts/apps/`)

```
wolts/apps/{name}/
├─ woltspace.json       # manifest (start command, port, description)
└─ ...                  # app source code
```

---

## Known Issues & Future Optimization Notes

### Complexity hotspots
- **core.py (~1100 lines)** — agent loop + all 13 tool implementations in one file. Tool functions could be extracted.

### Architecture concerns
- **Session lifecycle is implicit** — no state machine, relies on tmux existence checks. Reconciliation is reactive, not proactive.
- **`_send_ack` only handles Telegram** — Slack users get no "🦫 on it" ack when sessions spawn.
- **History window is fixed** — `MAX_HISTORY = 20` pairs for Telegram, Slack pulls full thread from API (inconsistent).
- **Single-wolt Telegram** — one wolt owns the bot at a time. Den replies lose context (#218). Chat-per-wolt is the path forward (#184).
- **Skill drift** — platform skills copied at wolt creation, never synced. Existing wolts run stale skills (#173).
- **App proxy links** — internal links break for multi-page apps behind `/app/{name}/` prefix (#212).

### Things that work well
- **Session registry** — one JSON file per session per wolt, queryable with `ls` and `cat`.
- **Den reply routing** — footer embeds session URL, adapter extracts it, message goes directly to tmux.
- **Per-wolt livereload** — file watcher + WebSocket broadcast + injected script = instant updates. Scoped per wolt.
- **Creature system** — simple model selection via metaphor (otter/beaver/raccoon).
- **Skills** — platform defaults baked in, wolt overrides win. Clean layering.
- **Filesystem as database** — paths are queries, files are records, dirs are indexes, cleanup is `rm`.

### Potential optimizations
- [ ] Extract tool implementations from core.py into separate files
- [ ] Add session TTL / auto-cleanup for orphaned session files
- [ ] Add Slack ack messages for session spawns
- [ ] Consider event-driven viewport updates (SSE/WS) instead of 2s polling
- [ ] Session state machine with explicit transitions
- [ ] Chat-per-wolt Telegram architecture (#184)
- [ ] Skill inheritance — platform skills vs local overrides (#173)

---

*Last updated 2026-03-24. This is a living document — add notes as you go.*
