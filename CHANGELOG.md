# Changelog

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
