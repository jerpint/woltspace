# Woltspace Roadmap

Last updated: 2026-03-14

---

## Recently Shipped

### Python Server Migration (PR #16)
FastAPI server is now the default runtime on port 3000. Node `server.js` kept for rollback. TUI service (xterm.js WebSocket) stays on Node at port 3001. Single-language goal for bot + server is ~80% there.

**Still to do:** Rewrite `woltspace` CLI from bash to Python (click). Mounts config needs a documented home.

### Test Framework (PR #13)
105 tests across 6 tiers, all green (~2min full run). Covers unit → session lifecycle → server health → closed-loop → integration → agent (haiku-in-the-loop). True e2e test: haiku spawns beaver → writes HTML → verified on disk → pushed to viewport.

**Still to do:** Run tests from outside the container (meta debugging). Versioning with tagged releases. CI pipeline.

### Project Isolation — Phases 1 & 2 (PR #17)
Users build in `wolt/projects/`, never touch `/workspace/woltspace/`. Guardrails at template, platform, and session-injection levels. Phase 2 added: project serving, bot tool `project=` scoping, `new-project` and `projects` skills, `migrate-to-projects` recovery skill. 38 isolation-specific tests.

**Still to do (Phase 3):** Mount `/workspace/woltspace/` as read-only in container (filesystem-level enforcement). This is the north star — zero chance of platform drift.

### Session Infrastructure (PRs #10, #14)
Centralized session registry replaced tmux-based session tracking. Session revival now picks the correct conversation.

### Bot & Creature Routing (PRs #8, #11)
Creatures, notify, tool logging cleaned up. Explicit user creature choice is respected. Dog is now the lodge creature (replacing otter).

### Build & Init (PRs #18, #19)
Claude CLI install cached in isolated Docker build stage (faster rebuilds). `woltspace` auto-added to PATH during init.

---

## Up Next

### 1. Woltspace-as-a-Project + Parallel Development (Priority: High — in progress)

**What:** Treat woltspace development like any other project. Dev clone lives in `wolt/projects/woltspace/`, worktrees enable parallel sessions.

- `wolt/projects/woltspace/` = dev clone (sessions work here)
- `/workspace/woltspace/` = production (mounted, serves tunnel, read-only in practice)
- Parallel work via `git worktree add` inside the dev clone
- Each session pushes a branch, creates a PR — never edits main directly
- Human reviews on phone, merges, rebuilds when ready
- Raccoons orchestrate parallel dispatch — not Haiku

**Status:** Dev clone set up, flow documented in `DEV_FLOW.md`. Worktrees verified working.

**Still to do:** Raccoon orchestration for multi-task requests (multiple `claude_code` calls). Supersedes the original `WORKTREE_PLAN.md` — no custom tooling needed, just projects + git.

**Why:** Dogfoods the project isolation system on the hardest case. If it works for woltspace itself, it works for everything. Unlocks parallel development without custom infra.

---

### 2. Python CLI Rewrite (Priority: High)

**What:** Rewrite `woltspace` CLI from bash to Python with [click](https://click.palletsprojects.com/).

- Current bash script is hard to read and maintain
- Server is already Python — CLI should match
- Mounts config gets a clear, documented home

**Why:** Completes the Python migration. One language for bot + server + CLI.

---

### 3. CI Pipeline (Priority: High)

**What:** Automated test runs on push/PR.

- Run the existing 105-test suite in CI (GitHub Actions)
- Run from outside the container — spin up clean, test, tear down
- Block merges on test failure
- After CI is solid: tagged releases, semantic versioning

**Why:** Tests exist but only run manually. Need to close the loop so main never breaks.

---

### 4. Telegram Hardening (Priority: High)

**What:** Make Telegram the last line of defense — self-healing, auto-restart.

- If telegram bot dies, auto-restart in container (supervisor/systemd-style)
- Telegram must stay functional even when everything else is broken
- Dog can always spawn new sessions as a fallback
- Worst case: dog yolo-spawns a Claude to fix things

**Why:** If telegram goes down while user is away from computer, they're in the dark. It's the primary control channel.

---

### 5. Identity & Creatures (Priority: Medium)

**What:** Finish the creature identity split. Formalize the full creature roster.

- Lodge companion (Telegram) → **dog** 🐶 (haiku — loyal, constrained, always-on)
- Claude Code sessions → **beaver** (sonnet) or **raccoon** (opus)
- Creature routing shipped — dog is live as the lodge creature

**See:** [Creature Roles](#creature-roles) section below for the full roster.

**Why:** Single-identity-for-everything gets confused. Each creature has a clear, constrained role.

---

### 6. Skill Hierarchy (Priority: Medium)

**What:** Proper skill organization — global skills all wolts share, plus wolt-specific skills.

- Mirror Claude Code's user session structure
- Global skills baked into image
- Wolt-local skills override or extend
- Crons should be services that fire up Claudes with single roles — not hardcoded scripts like digest.mjs

**Why:** Current crons are too specific. Skills should be composable — users create their own via a meta-skill.

---

### 7. Configuration (Priority: Medium)

**What:** Single source of truth for non-secret config.

- `woltspace.config` (toml/yaml/json) alongside `.env`
- Secrets stay in `.env`, everything else in config
- Tunnel should be first-class config (not buried in env vars)

**Why:** Currently dumping too much in env vars. Need clear separation of secrets vs config.

---

### 8. Memory Cleanup (Priority: Medium)

**What:** Active cleanup of bot (dog) memories via cron.

- Periodically update context, clear/trim chat history
- Keep it fresh, lean, prevents cost explosion

**Why:** Chat history grows unbounded, increases API costs and degrades quality.

---

### 9. Observability (Priority: Medium)

**What:** Better tooling to debug the agent loop and message routing.

- Structured logging for the full flow: user → haiku → tool call → session → notify → user
- Currently hard to debug routing issues
- Tie into test framework — failed flows should produce debug-friendly traces

---

---

## Creature Roles

The colony is expanding. Each creature has a single, clear role. Not all are built yet — this is the intended roster.

### Active (shipped)

| Creature | Emoji | Model | Role |
|----------|-------|-------|------|
| **dog** | 🐶 | haiku | Lodge companion — always-on, loyal, routes requests, first to hear |
| **beaver** | 🦫 | sonnet | Builder — Claude Code sessions, coding, grunt work |
| **raccoon** | 🦝 | opus | Orchestrator — complex reasoning, multi-task dispatch |

### Planned (scaffolded, not yet built)

| Creature | Emoji | Role | Entry Point |
|----------|-------|------|-------------|
| **wolf** | 🐺 | Cron & scheduler — runs the pack's routines. Fires tasks on schedule, tracks cadence. | `container/creatures/wolf.py` |
| **spider** | 🕷️ | Headless browser — crawls, scrapes, watches the web. Playwright-backed, quiet and fast. | `container/creatures/spider.py` |
| **bear** | 🐻 | Safety & validation — guards the den. Reviews outputs, flags risks, enforces boundaries. | `container/creatures/bear.py` |
| **panda** | 🐼 | Daily reminders & zen notifications — gentle, unhurried. Surfaces what matters, when it matters. | `container/creatures/panda.py` |

### Design Principles

- **One role per creature.** Creatures don't overlap. Dog fetches; wolf schedules; spider crawls; bear validates; panda nudges.
- **Model is separate from role.** A creature's model (haiku/sonnet/opus) is chosen for its tempo, not its identity. Wolf might be haiku — fast, lightweight, always-on. Bear might need sonnet for careful judgment.
- **Entry points are services or session types.** Some creatures (wolf, panda) are background services. Others (spider, bear) are invoked per-task. Dog is the Telegram adapter itself.
- **Creatures share the registry.** All sessions — regardless of creature — write to `.state/registry/`. Routing, status, and wolt are creature-agnostic fields.

---

## North Star / Exploratory

### Read-only Platform Mount (Phase 3 of Isolation)
Mount `/workspace/woltspace/` as read-only in container. Filesystem-level enforcement, zero trust. The ultimate guarantee that user work and platform code never collide.

### Multi-wolt Collaboration
- Wolts can enter other woltspaces as guests with limited roles/context
- Two wolts collaborate on shared projects
- Permission system or shared woltspaces

### Public Showcase & Pixel Art
- Root of `/public/` shows animated pixel art of wolts doing wolt things
- Reflects actual current state of woltspace
- Inspired by PostHog's OS-emulator website — windows are viewports, click a wolt to see their project
- `/public/` shows what wolts have built, how you organized your space

### Wolt Blog
- Give neowolt his blog back so he can post — but not all memories/context publicly

---

## Misc Notes

- **PR style:** Lead with a lore-flavored headline. Introduce each change with personality, not just bullets. Technical details go in a "What's in this PR" section. Keep it descriptive but fun — worth sharing with friends, not just reviewers.
- `manifesto` naming → use lore-themed words instead
- `HUMANS.md` should be oriented to devs who want to hack on it, with mermaid diagrams
- `sparks/` will likely be deprecated
- Session configs should be source of truth, not tmux names + env vars
- `wolt.json` manifest — reconsider format
- Update public lore, spruce up website with fun images and sprites
- Templates for new wolts should be more fun out of the gate
- Tool handlers should always return success/error flag + message explaining error
