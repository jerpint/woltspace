# Versioning

Woltspace follows semantic versioning: **vMAJOR.MINOR.PATCH**

## Version digits

### PATCH (v0.0.X) — safe updates
Non-breaking changes. New features, bug fixes, UI tweaks, new creature behaviors. The update skill installs these with a quick sanity check. No migration needed.

Examples: new dog acks, favicon change, wolf catch-up logic, skill improvements.

### MINOR (v0.X.0) — breaking changes, migration required
Changes that require user action. New env vars, renamed files, changed config formats, removed features. The update skill walks the user through the migration.

Each minor bump ships a migration document at `container/migrations/vX.Y.0.md` — prose a wolt performs with consent. It rides inside the wheel, so it arrives with the code it migrates to and the update skill reads it *after* the install.

Examples: config schema change, new required env var, creature API change, state format migration.

### MAJOR (vX.0.0) — platform overhaul
Rare. Fundamental architecture changes. Reserved for when the platform shape changes significantly.

Examples: new container architecture, complete rewrite of session system, breaking API changes across all creatures.

## Files

- `pyproject.toml`, `tui/package.json` — the Python and TUI versions; `container/Dockerfile` pins the same two
- `.version` — not tracked: the image build writes the installed version into the image, and `woltspace rebuild` reads it from the running container
- `container/migrations/` — migration docs for minor/major bumps, shipped in the wheel

## Release workflow

Releases publish from `main` or `release/X.Y` through a workflow the maintainer runs and approves: see [docs/releasing.md](docs/releasing.md). Release candidates use `X.Y.ZrcN`, and the tags and GitHub releases are created by that workflow.

## How updates use versions

`woltspace update` reads PyPI and takes the newest stable version, or a pre-release with `--pre`: see [docs/updates.md](docs/updates.md). The update skill reviews the release notes and migrations it crosses and asks for consent where needed.
