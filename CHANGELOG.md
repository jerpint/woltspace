# Changelog

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
