# Versioning

Woltspace follows semantic versioning: **vMAJOR.MINOR.PATCH**

## Version digits

### PATCH (v0.0.X) — safe updates
Non-breaking changes. New features, bug fixes, UI tweaks, new creature behaviors. `/update` pulls these with a quick sanity check. No migration needed.

Examples: new dog acks, favicon change, wolf catch-up logic, skill improvements.

### MINOR (v0.X.0) — breaking changes, migration required
Changes that require user action. New env vars, renamed files, changed config formats, removed features. `/update` detects these and walks the user through the migration before pulling.

Each minor bump ships a migration document at `container/migrations/vX.Y.0.md` — prose a wolt performs with consent. It rides inside the wheel, so it arrives with the code it migrates to and the update skill reads it *after* the install.

Examples: config schema change, new required env var, creature API change, state format migration.

### MAJOR (vX.0.0) — platform overhaul
Rare. Fundamental architecture changes. Reserved for when the platform shape changes significantly.

Examples: new container architecture, complete rewrite of session system, breaking API changes across all creatures.

## Files

- `pyproject.toml` — the package version
- `docs/release-notes.md` — notes for the release being prepared
- `container/migrations/` — migration docs for minor/major bumps, shipped in the wheel

## Release workflow

1. Merge PRs to main (or cherry-pick fixes into `release/X.Y` by PR)
2. When ready to cut a release:
   - Open a prep PR that bumps the version and rewrites `docs/release-notes.md`
   - If minor/major: add a migration document to `container/migrations/`
3. After the prep PR merges, run the `publish.yml` workflow from that branch. It waits for the maintainer's approval, publishes, then tags and creates the GitHub release itself. Never tag by hand.
4. Users update with `woltspace update`

## How /update uses versions

- Reads current `.version` from the running container
- Fetches latest tag from origin
- Compares: patch bump → safe pull with sanity check. Minor/major bump → flag it, show migration notes, require explicit confirmation.
