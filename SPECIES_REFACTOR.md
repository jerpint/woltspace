# Species Refactor — Next Steps

Branch: `refactor-species` ← `nw/species-architecture` (and future PRs)

**Design doc** (visual, open in viewport): `neowolt/wolt/site/species-architecture.html`
Served at `/species-architecture.html` via the tunnel — push to viewport with:
```bash
curl -s -X POST "http://localhost:3000/current?session=$(tmux display-message -p '#S')" \
  -H 'Content-Type: application/json' -d '{"url": "/species-architecture.html"}'
```

## What's done (PR #88)

- `species/` directory with `species.json` + `instructions.md` for all 6 types (rodent, wolf, dog, spider, bear, panda)
- `container/lib/species.py` — loading, resolution, `build_prompt()`, `get_creature_model()`, `get_creature_emoji()`
- `container/lib/wolts.py` — reads valid types and singleton constraints from species (not hardcoded)
- `container/bot/core.py` — `build_system_prompt()` delegates fully to `species.build_prompt("dog", vars)`, no hardcoded prompt text, no fallback dicts
- `container/entrypoint.sh` — 3-layer skill loading logic (platform → species → wolt)
- `species/dog/prompts/` — all bot prompt fragments as standalone `.md` files with `order.txt`

## What's left

### Phase 3 — Skill scoping
Skills haven't actually been moved yet. `entrypoint.sh` has the loading logic but `species/*/skills/` dirs are empty.

- Move general-purpose skills from `container/skills/` → `species/rodent/skills/` (music, digest, projects, apps, new-project, organize-context, etc.)
- Move wolf-specific skills → `species/wolf/skills/` (wolf skill)
- Keep only truly universal skills in `container/skills/` (notify, viewport, session-summary, update, worktui)
- Test that a rodent wolt gets rodent skills, a wolf wolt gets wolf skills

### Phase 4 — Session instruction composition
Right now only the dog (bot) loads species instructions. Session creatures (rodents, wolves) don't get their species `instructions.md` injected.

- Update `container/bin/run-session.sh` to prepend `species/{type}/instructions.md` to the session prompt
- The 3-layer CLAUDE.md story: platform base → species instructions → wolt CLAUDE.md (via `@` includes or prompt prepend)

### Phase 5 — Move wolf runtime
`container/creatures/wolf.py` should move to `species/wolf/runtime.py` to complete the species encapsulation. The entrypoint or a launcher script would start it from there.

### Nice-to-have (later)
- `species/dog/prompts/` overridable per wolt (layer 3) — a wolt could shadow `voice.md` to give their dog a different tone
- Avatar/character select page — reads `species/` + `wolts/*/wolt.json` and renders a card per wolt
- Formation spec — multi-species recipe format (like docker-compose for wolts)

## Key design decisions already made

- **Rodent is one species, three tiers** (otter/beaver/raccoon = haiku/sonnet/opus) — not separate species
- **Daemons and bots are singleton species** (wolf, dog) — enforced via `species.json singleton: true`
- **You never talk to a daemon** — rodent sessions configure them by editing their config files
- **No fallback dicts** — if species/ is missing it's a setup problem, not something to paper over
- **Prompt composition owned by species** — `species/dog/prompts/order.txt` defines loading order, core.py is just a caller
