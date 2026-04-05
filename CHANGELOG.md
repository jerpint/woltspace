# Changelog

## v0.4.4 (2026-04-05)

- **Session resume** — `resume_session()` handles 3 paths: claude running (send keys), claude exited (restart with `--resume` in pane), tmux dead (respawn with `--resume`). (#241)
- **Stop/resume lifecycle** — `POST /sessions/{name}/stop` and `POST /sessions/{name}/resume` endpoints. Lodge UI shows always-visible stop/play buttons on session rows.
- **Auto-resume in TUI** — opening a dead session in split.html automatically resumes it instead of showing a blank terminal.
- **tui-service fix** — stops creating bare tmux shells for non-main sessions. Fixes "wclaude: command not found" on resumed sessions.
- **UUID session IDs** — `run-session.sh` generates a UUID for `--session-id` (Claude Code requirement) and stores it in the registry. `--name` flag gives sessions a human-readable name.
- **Running-only filter** — lodge sessions view has a "Running only" toggle to filter to live sessions.
- **Vulture fixed** for per-wolt `.state/sessions/` model (was broken since state refactor).

## v0.4.3 (2026-04-05)

- **Streaming subdomain proxy** — replaced buffered `httpx.request()` with `client.send(stream=True)` + `StreamingResponse`. Fixes SSE and chunked responses for projects served via `*.localhost:7777`.
- **WebSocket proxy** — catch-all WS route for `*.localhost` subdomains, proxies to project port. Passes subprotocols through (Vite HMR `"vite-ping"`). Fixes live reload for Vite/Astro projects.

## v0.4.2 (2026-04-03)

- **Lodge redesign** — sidebar dashboard with wolt roster, project grid, sessions view grouped by wolt. Pixel art forest scene with procedural landscape, animated clouds, and river. (#255)
- Time-based greeting + rotating quotes on home view.
- Session count badge on nav.
- Wolt card tooltips (role + description) and site links.

## v0.4.1 (2026-04-02)

- **Fix stale skill names** from `woltspace-*` rename — `create-wolt` and `telegram` references in `app.py`, `onboard.html`, `entrypoint_setup.py`. (#256)

## v0.4.0 (2026-03-31)

- **Skill inheritance** — all platform skills renamed to `woltspace-*` prefix (e.g. `start-chat` → `woltspace-start-chat`). Wolt-owned skills are never touched. (#173)
- **Boot sync covers all wolts** — `sync_all_wolt_skills()` replaces `copy_skills()`. Every wolt with `.claude/skills/` gets fresh platform skills on every boot.
- **CLAUDE.md platform section** — auto-managed block at the top of every wolt's CLAUDE.md with platform rules, regenerated on boot. Wolt content below the markers is preserved.
- **`/woltspace-update` re-syncs** — after pulling, skills and CLAUDE.md are synced immediately without needing a restart.
- Removed `music` and `digest` from platform (wolts own their evolved copies).
- Legacy skills (`update-2026-03-13`, `migrate-to-projects`) moved to `legacy/` and no longer synced.

**Migration:** Old unprefixed platform skills must be removed from each wolt's `.claude/skills/`. See `docs/migrations/v0.4.0.md` for the blueprint — `/woltspace-update` handles it automatically.

## v0.3.0 (2026-03-24)

- **Wolt-centric state model** — runtime state moved from a single shared `.state/` dump to per-wolt `wolts/{wolt}/.state/` directories. Global platform state lives at `wolts/.space/`. (#228)
- **`lib/paths.py`** — central path helpers for all state locations. No more hardcoded `.state/` paths scattered across the codebase.
- **Unified test runner** — top-level `pyproject.toml` resolves all deps (server + bot). Run tests with `uv run --extra test pytest test/`.
- **Stale PID fix** — site health checks now probe the port directly instead of trusting `os.kill(pid, 0)`. Fixes stuck "waking up" when a PID gets reused. (#217)
- **Dead code removed** — `status.json` singleton, `GET /status` endpoint, wolf-wolt discovery functions, `read_status()`/`write_status()`.
- **gh-app-token fix** — shebang points to bot venv Python (has PyJWT).
- **Reload optimization** — uvicorn reload watches specific dirs to avoid useless restarts.

**Migration:** After updating, old `wolts/.state/` and `neowolt/.state/` directories are no longer read. Safe to delete after confirming everything works. Tunnel URL has a backwards-compat fallback during transition.

## v0.2.2 (2026-03-22)

- **Distributed wolf scheduler** — each wolt owns its own crons in `wolt/wolf.json`. Wolf scans all wolts and dispatches sessions for the owning wolt. (#215, #221)
- Simplified cron schema: `name` + `schedule`/`at` + `prompt` + optional `notify`. Removed old `action`/`script`/`skill`/`creature`/`timezone` fields.
- One-off crons via `"at"` field — fires once, auto-deletes from wolf.json.
- Nameless wolf — notifications now show `🐺 *Howl*` with creature emoji for the wolt being woken up.
- Rewritten `/wolf` skill for the new distributed model.
- Start-chat modes now mention `/wolf` for scheduling.

**Action required:** The `/wolf` skill has been rewritten. Existing wolts have a stale copy from the old model. After updating, replace it for each wolt:
```bash
cp -r /workspace/woltspace/container/skills/wolf/ /workspace/wolts/<wolt-name>/.claude/skills/wolf/
```

## v0.2.1 (2026-03-21)

- Credential symlink fix — `.claude/credentials.json` copied instead of symlinked to prevent atomic-replace breakage. (#216)

## v0.2.0 (2026-03-20)

- Wolt sites with per-wolt livereload (#198)
- Projects system — woltspace.json schema, API routes, proxy, lodge cards (#183, #185, #187, #191)
- Lodge redesign — wolt cards, gnaw buttons, info tooltips (#163)
- Session routing — unified spawning from lodge/telegram/slack (#168)
- Type system — raccoon/beaver/otter with creature-derived models (#169)
- Wakeup template + instant viewport (#199)
- Fast uvicorn reload (#203)

See release notes: https://github.com/jerpint/woltspace/releases/tag/v0.2.0

## v0.1.0 (2026-03-17)

- GitHub App auth — replace banned PAT with proper GitHub App (`container/bin/gh-app-token`) (#147)
- python-dotenv — replace fragile hand-rolled .env parser with standard library (#148)
- /update dep sync — automatically run `uv sync` after pulling to prevent import crashes (#149)
- /create-github-bot skill — walks new users through GitHub App setup (#147)

**Migration required:** run `migrations/v0.1.0.sh` (syncs new Python dependencies). The /update skill handles this automatically.

## v0.0.2 (2026-03-17)

- Playful dog acks — tail wags, head tilts, tippy taps instead of generic "on it..." (#112)
- Inline beaver favicon — can't be shadowed by wolt site files (#119)
- Wolf catch-up logic — fire missed crons on startup after container restart (#109)
- Release system — VERSIONING.md, CHANGELOG.md, version-aware /update skill (#116)
- Migration guide copied to migrations/v0.0.1.md (#117)

## v0.0.1 (2026-03-16)

First versioned release. Architecture refactor: git clone in Dockerfile, single mount, port 7777, simplified CLI, backup system, smoke tests. See PR #104.
