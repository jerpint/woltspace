# Woltspace Migration Guide: main → refactor-init

> **Audience:** A Claude session (or human) running on the **host machine**, outside the container. Copy-paste this entire file into a local Claude session.
>
> **How this works:** Two phases. Phase 1 is a **read-only audit** — it touches nothing, modifies nothing, stops nothing. It produces a report. You send that report to the project maintainer for review. Only after approval do you run Phase 2, which performs the actual migration.

---

## What changed (summary)

| Area | main (old) | refactor-init (new) | Risk |
|------|-----------|---------------------|------|
| Wolts directory | `~/wolts` | `~/.woltspace/wolts` | **HIGH** — CLI won't find existing wolts |
| Host port | `4444` | `7777` | Bookmarks/scripts break |
| Internal port | `3000` | `7777` | Container-internal references break |
| Tunnel env var | `ENABLE_TUNNEL` | `WOLTSPACE_PUBLIC_TUNNEL` | **SECURITY** — disabled tunnel may re-enable |
| Dev mode flag | `--dev` | `--local` / `--branch` | Workflow change |
| Woltspace in container | Mounted from host (`-v`) | Git clone inside image | `/update` still works; image goes stale until rebuild |

---

## Phase 1: Read-only audit

**This phase touches NOTHING. No files are modified, no containers are stopped, no backups are created.** It only reads and reports.

Run every command below and compile the results into a single report.

### 1.1 Locate wolts

```bash
# Check the default locations and the env var
echo "=== WOLTS LOCATION ==="
echo "WOLTS_DIR env: ${WOLTS_DIR:-<not set>}"
echo ""

# Check old default location (main)
if [ -d "$HOME/wolts" ]; then
  echo "~/wolts EXISTS"
  echo "  Wolts found:"
  for d in "$HOME/wolts"/*/wolt; do
    [ -d "$d" ] && echo "    $(basename $(dirname $d))"
  done
  echo "  Size: $(du -sh "$HOME/wolts" 2>/dev/null | cut -f1)"
  echo "  .env: $([ -f "$HOME/wolts/.env" ] && echo 'present' || echo 'MISSING')"
  echo "  woltspace.json: $([ -f "$HOME/wolts/woltspace.json" ] && echo 'present' || echo 'MISSING')"
else
  echo "~/wolts DOES NOT EXIST"
fi
echo ""

# Check new default location (refactor-init)
if [ -d "$HOME/.woltspace/wolts" ]; then
  echo "~/.woltspace/wolts EXISTS (new location already has data)"
  ls "$HOME/.woltspace/wolts"/*/wolt/wolt.json 2>/dev/null
else
  echo "~/.woltspace/wolts does not exist (expected for pre-migration)"
fi
```

### 1.2 Container state

```bash
echo "=== CONTAINER STATE ==="
# Running containers
echo "Running:"
docker ps --filter name=woltspace --format '  {{.Names}}  {{.Status}}  ports={{.Ports}}' 2>/dev/null || echo "  (docker not available)"
echo ""

# Stopped containers
echo "Stopped:"
docker ps -a --filter name=woltspace --filter status=exited --format '  {{.Names}}  {{.Status}}' 2>/dev/null || echo "  (none)"
echo ""

# Port mapping
echo "Port mapping:"
docker port woltspace 2>/dev/null || echo "  (no running container)"
echo ""

# Image info
echo "Image:"
docker image inspect woltspace --format '  Created: {{.Created}}  Size: {{.Size}}' 2>/dev/null || echo "  (no woltspace image)"
```

### 1.3 Woltspace repo state

```bash
echo "=== WOLTSPACE REPO ==="

# Find the repo — check common locations
WOLTSPACE_REPO=""
for candidate in "$HOME/woltspace" "$HOME/.woltspace/woltspace" "/workspace/woltspace" "$(dirname $(which woltspace 2>/dev/null))"; do
  if [ -d "$candidate/.git" ] || [ -f "$candidate/woltspace" ]; then
    WOLTSPACE_REPO="$candidate"
    break
  fi
done

if [ -z "$WOLTSPACE_REPO" ]; then
  echo "Could not find woltspace repo. Check manually."
else
  echo "Repo location: $WOLTSPACE_REPO"
  echo "Current branch: $(cd "$WOLTSPACE_REPO" && git branch --show-current)"
  echo "Last commit: $(cd "$WOLTSPACE_REPO" && git log --oneline -1)"
  echo ""

  # CRITICAL: check for uncommitted changes (drift from wolts editing platform code)
  echo "--- Uncommitted changes (drift check) ---"
  DRIFT=$(cd "$WOLTSPACE_REPO" && git status --short)
  if [ -z "$DRIFT" ]; then
    echo "  CLEAN — no drift detected"
  else
    echo "  WARNING: uncommitted changes found!"
    echo "$DRIFT" | while read line; do echo "    $line"; done
    echo ""
    echo "  --- Diff of changed files ---"
    cd "$WOLTSPACE_REPO" && git diff --stat
    echo ""
    # Categorize the changes
    echo "  --- Risk assessment ---"
    cd "$WOLTSPACE_REPO" && git status --short | while read status file; do
      case "$file" in
        container/entrypoint.sh|container/entrypoint_setup.py|server.js|server/*|woltspace)
          echo "    HIGH RISK: $file — platform infrastructure"
          ;;
        container/bot/*|container/creatures/*)
          echo "    MEDIUM RISK: $file — bot/creature code"
          ;;
        container/skills/*|CLAUDE.md|HUMANS.md|*.md)
          echo "    LOW RISK: $file — skills/docs (likely auto-updated)"
          ;;
        *)
          echo "    UNKNOWN: $file — review manually"
          ;;
      esac
    done
  fi
fi
```

### 1.4 Environment variables

```bash
echo "=== ENV FILE AUDIT ==="
WOLTS_DIR="${WOLTS_DIR:-$HOME/wolts}"
ENV_FILE="$WOLTS_DIR/.env"

if [ ! -f "$ENV_FILE" ]; then
  echo "No .env file found at $ENV_FILE"
else
  echo "Location: $ENV_FILE"
  echo ""

  # Check for vars that need renaming
  echo "--- Variables that need migration ---"

  if grep -q 'ENABLE_TUNNEL' "$ENV_FILE" 2>/dev/null; then
    echo "  RENAME NEEDED: $(grep 'ENABLE_TUNNEL' "$ENV_FILE")"
    echo "    → should become: $(grep 'ENABLE_TUNNEL' "$ENV_FILE" | sed 's/ENABLE_TUNNEL/WOLTSPACE_PUBLIC_TUNNEL/')"
    # Check if tunnel was explicitly disabled — security risk if missed
    if grep -q '^ENABLE_TUNNEL=false' "$ENV_FILE"; then
      echo "    ⚠ SECURITY: tunnel was DISABLED. If this rename is missed, new code defaults to PUBLIC tunnel ENABLED."
    fi
  else
    echo "  ENABLE_TUNNEL: not found (check if WOLTSPACE_PUBLIC_TUNNEL already set)"
    if grep -q 'WOLTSPACE_PUBLIC_TUNNEL' "$ENV_FILE"; then
      echo "    Already migrated: $(grep 'WOLTSPACE_PUBLIC_TUNNEL' "$ENV_FILE")"
    else
      echo "    Neither variable found — new code will default to WOLTSPACE_PUBLIC_TUNNEL=true (tunnel ENABLED)"
    fi
  fi
  echo ""

  # Check for port references
  echo "--- Port references ---"
  if grep -q '4444\|:3000' "$ENV_FILE"; then
    echo "  Found old port references:"
    grep -n '4444\|:3000' "$ENV_FILE" | while read line; do echo "    $line"; done
  else
    echo "  No old port references found (OK)"
  fi
  echo ""

  # Check required vars are set
  echo "--- Required variables ---"
  for var in CLAUDE_CODE_OAUTH_TOKEN; do
    val=$(grep "^${var}=" "$ENV_FILE" 2>/dev/null | cut -d= -f2-)
    if [ -n "$val" ] && [ "$val" != "your_oauth_token_here" ]; then
      echo "  $var: set ($(echo "$val" | cut -c1-8)...)"
    else
      echo "  $var: NOT SET or placeholder"
    fi
  done
fi
```

### 1.5 Port conflicts

```bash
echo "=== PORT CHECK ==="
# Check if anything is already on port 7777 (the new port)
if command -v lsof &>/dev/null; then
  CONFLICT=$(lsof -i :7777 2>/dev/null | grep LISTEN)
  if [ -n "$CONFLICT" ]; then
    echo "  WARNING: port 7777 is already in use!"
    echo "  $CONFLICT"
  else
    echo "  Port 7777: available (OK)"
  fi
elif command -v ss &>/dev/null; then
  CONFLICT=$(ss -tlnp 2>/dev/null | grep ':7777 ')
  if [ -n "$CONFLICT" ]; then
    echo "  WARNING: port 7777 is already in use!"
    echo "  $CONFLICT"
  else
    echo "  Port 7777: available (OK)"
  fi
else
  echo "  (cannot check — neither lsof nor ss available)"
fi

# Check what's on port 4444 (the old port)
echo ""
if command -v lsof &>/dev/null; then
  OLD_PORT=$(lsof -i :4444 2>/dev/null | grep LISTEN)
  if [ -n "$OLD_PORT" ]; then
    echo "  Port 4444 (old): still in use — this is the current container"
  else
    echo "  Port 4444 (old): free"
  fi
fi
```

### 1.6 Shell profile check

```bash
echo "=== SHELL PROFILE ==="
# Check if WOLTS_DIR is already exported in shell config
for rc in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.bash_profile"; do
  if [ -f "$rc" ]; then
    WD=$(grep 'WOLTS_DIR' "$rc" 2>/dev/null)
    WS=$(grep 'woltspace' "$rc" 2>/dev/null)
    if [ -n "$WD" ] || [ -n "$WS" ]; then
      echo "  $(basename $rc):"
      [ -n "$WD" ] && echo "    WOLTS_DIR: $WD"
      [ -n "$WS" ] && echo "    woltspace: $WS"
    fi
  fi
done
echo ""
echo "  Current PATH woltspace: $(which woltspace 2>/dev/null || echo 'not on PATH')"
```

### 1.7 Compile the report

After running all the above, compile the results into a single block:

```
=== MIGRATION AUDIT REPORT ===

Wolts location: [~/wolts | ~/.woltspace/wolts | custom]
Wolts found: [list of wolt names]
Wolts size: [size]
.env present: [yes/no]
woltspace.json present: [yes/no]

Container: [running on port XXXX | stopped | not found]
Image: [present, created DATE | not found]

Repo location: [path]
Repo branch: [main | other]
Repo drift: [CLEAN | changes found — see details]
  [if drift: list changed files with risk levels]

Env migration needed:
  ENABLE_TUNNEL → WOLTSPACE_PUBLIC_TUNNEL: [yes, value=X | no | not set — DEFAULTS TO PUBLIC]

Port 7777: [available | CONFLICT]
WOLTS_DIR in shell profile: [yes | no — needs adding]

OVERALL: [SAFE TO PROCEED | NEEDS ATTENTION — list blockers]
```

**Send this report to the maintainer. Do not proceed to Phase 2 until they confirm.**

---

## Phase 2: Migration (requires approval)

> **Only run this after the maintainer has reviewed the Phase 1 audit report and given explicit approval.**

### 2.1 Back up everything

One command snapshots the container (runtime, deps, code) and copies the wolts directory. Both get the same tag so they're paired for recovery.

```bash
woltspace backup pre-migration
```

This creates:
- **Container image:** `woltspace-backup:pre-migration`
- **Wolts copy:** `~/.woltspace/wolts-backup-pre-migration`

The command prints a restore snippet at the end — save it. **Keep both until you've confirmed the migration works.**

### 2.3 Handle repo drift (if any found in audit)

If the Phase 1 audit found uncommitted changes in the woltspace repo:

```bash
cd /path/to/woltspace  # use the actual path from the audit

# Save changes to a branch before they're lost
git checkout -b backup/pre-migration
git add -A
git commit -m "pre-migration state — drift from main"
git checkout main
```

If the audit found the repo was **clean**, skip this step.

### 2.4 Stop the container

```bash
docker stop woltspace 2>/dev/null
docker rm woltspace 2>/dev/null
```

### 2.5 Move wolts to new location

The new default is `~/.woltspace/wolts`. Move your wolts there so the CLI finds them without env vars or symlinks.

```bash
WOLTS_DIR="${WOLTS_DIR:-$HOME/wolts}"

# Move to new location (backup is already saved in 2.2)
mkdir -p "$HOME/.woltspace"
mv "$WOLTS_DIR" "$HOME/.woltspace/wolts"

# Verify
ls "$HOME/.woltspace/wolts"/*/wolt/wolt.json
echo "Wolts moved to ~/.woltspace/wolts"
```

### 2.6 Update .env

```bash
ENV_FILE="$HOME/.woltspace/wolts/.env"

# Rename tunnel variable (preserves the value)
if grep -q 'ENABLE_TUNNEL' "$ENV_FILE" 2>/dev/null; then
  sed -i.bak 's/ENABLE_TUNNEL/WOLTSPACE_PUBLIC_TUNNEL/g' "$ENV_FILE"
  echo "Renamed ENABLE_TUNNEL → WOLTSPACE_PUBLIC_TUNNEL"
fi

# If neither tunnel var exists, add one explicitly
if ! grep -q 'WOLTSPACE_PUBLIC_TUNNEL' "$ENV_FILE" 2>/dev/null; then
  echo "" >> "$ENV_FILE"
  echo "# Public tunnel (set false to use http://localhost:7777 only)" >> "$ENV_FILE"
  echo "WOLTSPACE_PUBLIC_TUNNEL=true" >> "$ENV_FILE"
  echo "Added WOLTSPACE_PUBLIC_TUNNEL=true (edit to =false if you want local only)"
fi

# Update port references in comments
sed -i 's/localhost:4444/localhost:7777/g' "$ENV_FILE" 2>/dev/null

# Verify
echo "Tunnel setting:"
grep 'WOLTSPACE_PUBLIC_TUNNEL' "$ENV_FILE"
```

### 2.7 Update and rebuild

```bash
cd /path/to/woltspace

# Ensure clean working tree
git status --short
# Should be empty (drift was handled in 2.3)

# Switch to new branch
git fetch origin
git checkout refactor-init
git pull origin refactor-init

# Rebuild
woltspace rebuild
```

### 2.8 Post-migration validation

```bash
echo "=== POST-MIGRATION CHECK ==="

# Container running?
echo -n "Container: "
docker ps --filter name=woltspace --format '{{.Names}} {{.Status}}' 2>/dev/null || echo "NOT RUNNING"

# Server responding?
echo -n "Server (port 7777): "
curl -s -o /dev/null -w "%{http_code}" http://localhost:7777/ 2>/dev/null || echo "NO RESPONSE"
echo ""

# Wolts visible?
echo "Wolts in container:"
docker exec woltspace ls /workspace/wolts/*/wolt/wolt.json 2>/dev/null || echo "  NONE FOUND"

# Active wolt
echo -n "Active wolt: "
docker exec woltspace printenv WOLT_NAME 2>/dev/null || echo "NOT SET"

# Tunnel
echo -n "Tunnel: "
TUNNEL_URL=$(cat "$HOME/.woltspace/wolts/.state/tunnel-url" 2>/dev/null)
if [ -n "$TUNNEL_URL" ]; then
  echo "$TUNNEL_URL"
else
  echo "none (expected if disabled)"
fi

# Backup reminder
echo ""
echo "Migration complete. Your backups:"
echo "  Container image: woltspace-backup:pre-migration"
echo "  Wolts data: ~/.woltspace/$BACKUP_NAME"
echo ""
echo "Keep these until you're confident everything works."
echo "To remove later: docker rmi woltspace-backup:pre-migration && rm -rf ~/.woltspace/$BACKUP_NAME"
```

---

## Rollback

If anything went wrong, the backup tag ties everything together:

```bash
# Stop the broken container
docker stop woltspace 2>/dev/null
docker rm woltspace 2>/dev/null

# Option A: Restore from snapshot (fastest — exact runtime from before migration)
docker run -d --name woltspace \
  -v "$HOME/.woltspace/wolts-backup-pre-migration:/workspace/wolts:rw" \
  -p 7777:7777 \
  woltspace-backup:pre-migration

# Option B: Full restore to pre-migration state
# Move wolts back to old location
mv "$HOME/.woltspace/wolts-backup-pre-migration" "$HOME/wolts"

# Switch woltspace back to main
cd /path/to/woltspace
git checkout main

# Rebuild on main
woltspace rebuild

# Verify old setup works
docker ps --filter name=woltspace
curl -s http://localhost:7777/
```

---

## Quick reference: old → new

```
~/wolts                        → ~/.woltspace/wolts (or set WOLTS_DIR)
localhost:4444                 → localhost:7777
ENABLE_TUNNEL=false            → WOLTSPACE_PUBLIC_TUNNEL=false
woltspace start --dev          → woltspace start --local
woltspace rebuild --dev        → woltspace rebuild --local
woltspace restart              → woltspace stop && woltspace start
/update (inside container)     → still works (git pull inside clone)
```

## Known limitations after migration

1. **Image goes stale after in-app updates** — `/update` still works (the container has a git clone), but after pulling, the Docker image is stale vs the running container. Run `woltspace rebuild` from the host to re-bake the image for future cold starts (not required for the current session).
2. **No live-mount of platform code** — on `main`, woltspace was mounted from the host so edits were instant. Now it's a clone inside the container. For iterative platform development, use `--local` builds.
3. **Dev mode changed** — `--local` builds from your local repo into the image (COPY, not mount). More rebuilds, better isolation.
4. **Deploy key mount removed** — use `GH_PAT_TOKEN` in `.env` instead of SSH deploy keys.
