# Woltspace Roadmap

Organized from jerpint's airplane-mode notes (2026-03-14).

---

## 1. Python Migration (Priority: High)

**What:** Rewrite server.js and woltspace CLI in Python.

- `server.js` → FastAPI (most API/routing logic). Keep a thin Node service only for things that need it (xterm.js WebSocket, node-pty).
- `woltspace` CLI → Python with [click](https://click.palletsprojects.com/). Current bash is hard to read and maintain.
- Mounts config needs a clear, documented home (currently assumed `~/wolts/.env`).

**Why:** JS gets messy for jerpint to read. Python has better ecosystem for CLIs, easier to maintain. Single language for bot + server + CLI.

---

## 2. Testing & CI (Priority: High)

**What:** Closed-loop integration tests where Claude acts as a Telegram user.

- Spin up a clean container from outside
- Have Claude (or a test harness) interface directly with the Telegram bot
- Run health checks: create session, push to viewport, notify, kill session
- Verify everything works end-to-end after rebuild
- Consolidate all tests in one place (currently scattered: `test/`, `container/bot/test_*`)
- Decouple tests from tmux where possible — tmux should only persist terminals, not manage state

**Why:** Debugging from within the container fails for bigger lifts. Need a meta debugging routine: rebuild clean, test from outside, inspect logs. Main should never be broken.

**After tests are solid:** introduce versioning with tagged releases. Explore packaging (brew?).

---

## 3. Telegram Hardening (Priority: High)

**What:** Make Telegram the last line of defense — self-healing, auto-restart.

- If telegram bot dies, auto-restart in container
- Telegram must stay functional even when everything else is broken
- Dog (telegram bot) can always spawn new sessions as a fallback
- Worst case: dog yolo-spawns a Claude to fix things

**Why:** If telegram goes down while user is away from computer, they're in the dark. It's the primary control channel.

---

## 4. Identity Rethink (Priority: Medium)

**What:** Separate creature identities for telegram bot vs den sessions.

- Telegram bot → **dog** (based on Fuji ❤️). Loyal, obedient, runs tools when asked, fetches what you need. Limited abilities, no direct command execution.
- Claude Code sessions → **otter** (current identity, becomes a Haiku session)
- Dog is also a wolt, but with limited roles

**Why:** Current otter-for-everything gets confused. Dog has clear, constrained role.

---

## 5. Skill Hierarchy (Priority: Medium)

**What:** Proper skill organization — global skills all wolts share, plus wolt-specific skills.

- Mirror Claude Code's user session structure
- Global skills baked into image
- Wolt-local skills override or extend
- Crons should be services that fire up Claudes (headless or session) with single roles — not hardcoded scripts like digest.mjs

**Why:** Current digest.mjs is too specific. Crons should be generic — users create their own via a meta-skill.

---

## 6. Projects & Isolation (Priority: High — Phase 1 shipped)

**What:** Isolate user work from platform code. Users build in `wolt/projects/`, never touch `/workspace/woltspace/`.

**Phase 1 (shipped):**
- `wolt/projects/` directory as the standard location for all code projects
- CLAUDE.md guardrails at template, platform, and session-injection level that forbid editing platform code
- Clear instructions: code goes in `wolt/projects/`, static pages in `wolt/site/`

**Phase 2 (next):**
- Projects are discoverable, registered (like sessions), can be toggled on/off
- Bot tools get a `project=` parameter to scope sessions to a project directory
- Session registry tracks which project each session is working on
- Recommended stacks for easy projects (e.g. Python backend + SQLite + Vite)
- Git-tracked per project, pushable if user provides their git

**Phase 3 (future):**
- Mount `/workspace/woltspace/` as read-only in container (filesystem-level enforcement)
- North star: woltspace = place for tinkerers to build full-stack apps they can share from anywhere
- Apps don't scale by design — extract and deploy elsewhere when ready

**Why:** Users' Claude sessions drift into platform code, making it impossible to merge upstream woltspace updates. Isolation keeps user work portable and the platform upgradeable.

---

## 7. Configuration (Priority: Medium)

**What:** Single source of truth for non-secret config.

- `woltspace.config` (toml/yaml/json) alongside `.env`
- Secrets stay in `.env`, everything else in config
- Tunnel should be first-class config (not buried in env vars) — it opens a tunnel to user's computer

**Why:** Currently dumping too much in env vars. Need clear separation of secrets vs config.

---

## 8. Memory Cleanup (Priority: Medium)

**What:** Active cleanup of bot (dog) memories via cron.

- Periodically update context, clear/trim chat history
- Keep it fresh, lean, prevents cost explosion

**Why:** Chat history grows unbounded, increases API costs and degrades quality.

---

## 9. Observability (Priority: Medium)

**What:** Better tooling to debug the agent loop and message routing.

- Structured logging for the full flow: user → haiku → tool call → session → notify → user
- Currently hard to debug routing issues

---

## 10. Fun / North Star Ideas (Priority: Low — Exploratory)

### Multi-wolt collaboration
- Wolts can enter other woltspaces as guests with limited roles/context
- Two wolts collaborate on shared projects
- Maybe a permission system, maybe shared woltspaces they co-own

### Pixel art mini game
- Root of `/public/` shows animated pixel art of wolts doing wolt things
- Reflects actual current state of woltspace
- Simple Sims-like view of your wolts
- Inspired by PostHog's OS-emulator website — windows are viewports, click a wolt to see their project
- Part of the woltspace experience: care for how your space looks

### Public showcase
- `/public/` shows what wolts have built, how you organized your space
- Projects, wolts, everything discoverable

### Wolt blog
- Give neowolt his blog back so he can post — but not all memories/context publicly

---

## Misc Notes

- `manifesto` naming → use lore-themed words instead
- `HUMANS.md` should be oriented to devs who want to hack on it, with mermaid diagrams
- `sparks/` will likely be deprecated
- Session configs should be source of truth, not tmux names + env vars
- `wolt.json` manifest — reconsider format
- `memory/` naming → consider "cache" or "logs" for woltspace theming
- Update public lore, spruce up website with fun images and sprites
- `/viewport` assumptions should be re-checked against latest rewrites
- Templates for new wolts should be more fun out of the gate
- Tool handlers should always return success/error flag + message explaining error
