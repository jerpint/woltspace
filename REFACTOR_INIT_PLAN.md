# Refactor: woltspace init & CLI simplification

## Core principle

`~/.woltspace/wolts/` is the entire app state. The woltspace repo lives inside the container, not on the host. The host CLI is a dumb pipe to Docker.

## Installation

Everything lives under `~/.woltspace/`:

```
~/.woltspace/
  woltspace              ← the CLI script (single file)
  wolts/                 ← all wolt data (the only mount)
    .env
    woltspace.json
    neowolt/
    howlie/
```

Install via curl (no git, no sudo):
```bash
curl -fsSL https://woltspace.com/install.sh | bash
```

`install.sh` does:
1. `mkdir -p ~/.woltspace`
2. Download `woltspace` CLI to `~/.woltspace/woltspace`
3. Prompt to add `~/.woltspace` to PATH
4. Run `woltspace init`

Uninstall: `rm -rf ~/.woltspace` (wolt data is in `~/.woltspace/wolts/`, so user is warned)

`WOLTS_DIR` defaults to `~/.woltspace/wolts/`, overridable for custom locations.

## Architecture (new)

```
Host                                    Container
────                                    ─────────
~/.woltspace/wolts/ (only mount)   →    /workspace/wolts/
~/.woltspace/woltspace (CLI)            /workspace/woltspace/ (git clone during build)
```

- No git required on host
- No host-side repo clone
- No python3 required on host
- Only host dependency: Docker
- Hot reload always on (watchfiles, node --watch)
- Updates happen inside container via `/update` skill (git pull)
- `--dev` mode = staging branch, normal = main
- Backup: `~/.woltspace/wolts` + git ref = full restore, exact replica

## Localhost first

- Server binds to port 7777 immediately on boot
- Tunnel is optional, fails gracefully, never crashes the container
- If cloudflare is down, localhost:7777 always works

## CLI (simplified)

The `woltspace` script becomes ~100 lines. No JSON parsing, no python3, no config reading.

### init (fresh install)

```
woltspace init
  → splash screen, "name your wolt:" prompt
  → mkdir ~/.woltspace/wolts, create .env template
  → docker build + docker run
  → container handles: template copy, wolt.json, woltspace.json, git init, onboarding
```

### init (existing wolts detected)

```
woltspace init
  → "found 3 existing wolts: neowolt, howlie, fujiwolt"
  → skip naming, skip scaffold
  → docker build + docker run (idempotent)
```

### Subcommands

```
start       → docker run (or detect existing ~/wolts and boot)
stop        → docker stop
restart     → docker restart + show URL
rebuild     → docker build + restart
shell       → docker exec -it bash
chat        → docker exec -it claude
logs        → docker logs -f
```

### Removed

- `list` — folded into init detection of existing wolts
- `update` — happens inside container via /update skill
- `_read_config` / `_write_config` — no host-side JSON parsing
- `_resolve_active_wolt` — container reads woltspace.json itself

### Future

- `--version <ref>` flag on init/rebuild — build from specific git ref/tag for rollback
- `woltspace doctor` — vanilla claude session inside container for debugging

## _ensure_container (simplified)

Before:
```bash
docker run -d \
  --env-file "$env_file" \
  -v "$WOLTS_DIR:/workspace/wolts:rw" \
  -v "$WOLTS_DIR/.claude:/home/node/.claude:rw" \
  -v "$WOLTSPACE_DIR:/workspace/woltspace:rw" \
  $deploy_key_mount \
  -p 7777:7777 \
  -e WOLTS_DIR=/workspace/wolts \
  -e WOLT_NAME="$ACTIVE_WOLT" \
  -e DEV_MODE="$DEV_MODE" \
  -e CLAUDE_CODE_OAUTH_TOKEN="$oauth_token" \
  woltspace
```

After:
```bash
docker run -d \
  --name woltspace \
  --env-file "$WOLTS_DIR/.env" \
  -v "$WOLTS_DIR:/workspace/wolts:rw" \
  -p 7777:7777 \
  woltspace
```

One mount. One env file. Done.

## Entrypoint changes

- Dev dep reinstall section stays (for now, still needed when host mount is present)
- `entrypoint_setup.py` handles all config/identity (already done)
- Container resolves active wolt from woltspace.json (no host input needed)
- Tunnel crash never takes down container (already fixed — disown)
- TODO: `/wake` skill replaces hardcoded "hey $WOLT_NAME" greeting

## Dockerfile changes

- Add `git clone` of woltspace repo during build (when host mount is removed)
- Bake version: `RUN git rev-parse --short HEAD > /etc/woltspace-version`

## Migration path

1. First: simplify CLI + entrypoint with current mount setup (in progress)
2. Later: standalone CLI distribution (curl/brew), remove host repo mount, Dockerfile does git clone
