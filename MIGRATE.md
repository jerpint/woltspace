# Woltspace Migration Guide: main → refactor-init

> **Audience:** This document is for a Claude session (or human) running on the **host machine**, outside the container. Copy-paste this entire file into a local Claude session and ask it to walk you through the migration.
>
> **Goal:** Safely migrate an existing woltspace installation from `main` to the new `refactor-init` architecture without losing data.
>
> **Safety principle:** Back up first, validate everything, only proceed when clean.

---

## What changed (summary)

| Area | main (old) | refactor-init (new) | Risk |
|------|-----------|---------------------|------|
| Wolts directory | `~/wolts` | `~/.woltspace/wolts` | **HIGH** — CLI can't find existing wolts |
| Host port | `4444` | `7777` | Bookmarks/scripts break |
| Internal port | `3000` | `7777` | Container-internal references break |
| Tunnel env var | `ENABLE_TUNNEL` | `WOLTSPACE_PUBLIC_TUNNEL` | **SECURITY** — disabled tunnel may re-enable |
| Dev mode flag | `--dev` | `--local` / `--branch` | Workflow change |
| Woltspace in container | Mounted from host (`-v`) | Baked into image (git clone) | `/update` skill stops working |
| Container name | Hardcoded `woltspace` | `$WOLTSPACE_CONTAINER` (default: `woltspace`) | No risk |

---

## Step-by-step migration

### Step 0: Pre-flight checks

Before touching anything, confirm the current state:

```bash
# Where are your wolts?
echo "WOLTS_DIR: ${WOLTS_DIR:-~/wolts}"
ls "${WOLTS_DIR:-$HOME/wolts}"/*/wolt/wolt.json 2>/dev/null

# Is the container running?
docker ps --filter name=woltspace --format '{{.Names}} {{.Status}}'

# What branch is your woltspace repo on?
cd /path/to/woltspace && git branch --show-current

# What port is currently mapped?
docker port woltspace 2>/dev/null
```

**Record these values.** You'll need them if anything goes wrong.

---

### Step 1: Back up wolts directory

This is the most important step. Your wolts directory contains all identity, memory, site content, sparks, drafts, .env, and .claude config. Everything else is reconstructible.

```bash
# Determine current wolts location
WOLTS_DIR="${WOLTS_DIR:-$HOME/wolts}"

# Create timestamped backup
BACKUP_DIR="$HOME/wolts-backup-$(date +%Y%m%d-%H%M%S)"
cp -a "$WOLTS_DIR" "$BACKUP_DIR"

# Verify backup
echo "Backup created at: $BACKUP_DIR"
echo "Original size: $(du -sh "$WOLTS_DIR" | cut -f1)"
echo "Backup size:   $(du -sh "$BACKUP_DIR" | cut -f1)"

# Verify key files exist in backup
for f in "$BACKUP_DIR"/.env "$BACKUP_DIR"/woltspace.json; do
  [ -f "$f" ] && echo "  OK: $(basename $f)" || echo "  MISSING: $(basename $f)"
done
for d in "$BACKUP_DIR"/*/wolt; do
  [ -d "$d" ] && echo "  OK: $(basename $(dirname $d))/wolt/"
done
```

**Do not proceed until the backup is verified.** If sizes don't match or key files are missing, investigate before continuing.

---

### Step 2: Stop the running container

```bash
docker stop woltspace 2>/dev/null
docker rm woltspace 2>/dev/null
```

---

### Step 3: Check woltspace repo for wolt-caused drift

On `main`, the woltspace repo is mounted read-write into the container. Wolts (or Claude sessions) may have accidentally modified platform files. We need to check.

```bash
cd /path/to/woltspace  # wherever you cloned it

# Check for uncommitted changes
git status --short
```

**Evaluate the output:**

- **Clean (no output):** Safe to proceed.
- **Changes in `container/skills/`, `CLAUDE.md`, docs:** Likely harmless — skills or docs were auto-updated. Safe to discard with `git checkout -- .` after reviewing.
- **Changes in `container/entrypoint.sh`, `server.js`, `container/bot/`, `woltspace`:** These are platform code changes. **Do NOT discard without understanding them.** A wolt or Claude session may have patched something important. Review each change:

```bash
# Review each changed file
git diff

# If there are changes you want to preserve, stash them
git stash push -m "pre-migration-changes"
```

**If you find meaningful changes in platform code:**
1. Note what was changed and why (check recent Claude session logs if needed)
2. Stash or commit them to a branch: `git checkout -b backup/pre-migration && git add -A && git commit -m "pre-migration state"`
3. Return to your working branch: `git checkout main` (or whatever branch you were on)

---

### Step 4: Update .env for new variable names

```bash
WOLTS_DIR="${WOLTS_DIR:-$HOME/wolts}"
ENV_FILE="$WOLTS_DIR/.env"

# Check for the old tunnel variable
if grep -q '^ENABLE_TUNNEL=' "$ENV_FILE" 2>/dev/null; then
  OLD_VALUE=$(grep '^ENABLE_TUNNEL=' "$ENV_FILE" | cut -d= -f2-)
  echo "Found ENABLE_TUNNEL=$OLD_VALUE"
  echo "This needs to become WOLTSPACE_PUBLIC_TUNNEL=$OLD_VALUE"
fi

if grep -q '# ENABLE_TUNNEL=' "$ENV_FILE" 2>/dev/null; then
  echo "Found commented ENABLE_TUNNEL line — will need updating"
fi
```

**Apply the rename** (review before running):

```bash
# Preview the change
sed -n 's/ENABLE_TUNNEL/WOLTSPACE_PUBLIC_TUNNEL/p' "$ENV_FILE"

# Apply it
sed -i.bak 's/ENABLE_TUNNEL/WOLTSPACE_PUBLIC_TUNNEL/g' "$ENV_FILE"

# Also update the comment about the port if present
sed -i 's/localhost:4444/localhost:7777/g' "$ENV_FILE"

# Verify
grep -n 'TUNNEL\|4444\|7777' "$ENV_FILE"
```

**CRITICAL CHECK:** If the user had `ENABLE_TUNNEL=false` and this step is skipped, the new code defaults to `WOLTSPACE_PUBLIC_TUNNEL=true` and a public tunnel will be created. Verify:

```bash
grep 'WOLTSPACE_PUBLIC_TUNNEL' "$ENV_FILE" || echo "WARNING: No tunnel setting found — will default to PUBLIC tunnel enabled"
```

---

### Step 5: Decide on wolts directory location

You have three options:

**Option A: Keep wolts at `~/wolts` (recommended for existing users)**

Set the env var so the new CLI finds your wolts at the old path:

```bash
# Add to your shell profile (~/.zshrc, ~/.bashrc, ~/.bash_profile)
echo 'export WOLTS_DIR="$HOME/wolts"' >> ~/.zshrc  # or your shell's rc file
source ~/.zshrc
```

**Option B: Symlink (clean path, no data move)**

```bash
mkdir -p ~/.woltspace
ln -s ~/wolts ~/.woltspace/wolts
```

**Option C: Move wolts to new location**

```bash
mkdir -p ~/.woltspace
mv ~/wolts ~/.woltspace/wolts
# Update any scripts/aliases that reference ~/wolts
```

**Verify** whichever option you chose:

```bash
ls "${WOLTS_DIR:-$HOME/.woltspace/wolts}"/*/wolt/wolt.json
# Should list your wolt(s)
```

---

### Step 6: Update the woltspace repo

```bash
cd /path/to/woltspace

# Make sure working tree is clean (Step 3 should have handled this)
git status --short
# Should be empty

# Fetch and switch to the new branch
git fetch origin
git checkout refactor-init
git pull origin refactor-init
```

---

### Step 7: Rebuild and start

```bash
# Build the new image (this replaces the old one)
woltspace rebuild

# Or if this is a fresh start:
woltspace init
```

The new CLI will:
- Detect your existing wolts (if Step 5 was done correctly)
- Build a new Docker image with woltspace baked in
- Start the container on port 7777

---

### Step 8: Post-migration validation

```bash
# Container running?
docker ps --filter name=woltspace --format '{{.Names}} {{.Status}}'

# Server responding on new port?
curl -s -o /dev/null -w "%{http_code}" http://localhost:7777/

# Wolts visible inside container?
docker exec woltspace ls /workspace/wolts/*/wolt/wolt.json

# Check tunnel status (if enabled)
cat "${WOLTS_DIR:-$HOME/.woltspace/wolts}/.state/tunnel-url" 2>/dev/null || echo "No tunnel URL (expected if tunnel disabled)"

# Verify wolt identity survived
docker exec woltspace cat /workspace/wolts/$(docker exec woltspace printenv WOLT_NAME)/wolt/wolt.json
```

---

### Step 9: Update bookmarks and scripts

- `http://localhost:4444` → `http://localhost:7777`
- `woltspace start --dev` → `woltspace start --local` (or `woltspace rebuild --local`)
- `woltspace restart` → `woltspace stop && woltspace start`
- The `/update` skill inside the container no longer works for updating the platform. Instead, update from the host: `cd /path/to/woltspace && git pull && woltspace rebuild`

---

## Rollback plan

If anything goes wrong:

```bash
# Stop the new container
docker stop woltspace 2>/dev/null
docker rm woltspace 2>/dev/null

# Switch woltspace back to main
cd /path/to/woltspace
git checkout main

# Restore backup if needed
BACKUP_DIR="$HOME/wolts-backup-XXXXXXXX"  # use your actual backup dir
cp -a "$BACKUP_DIR/." "${WOLTS_DIR:-$HOME/wolts}/"

# Rebuild on main
woltspace rebuild

# Verify
docker ps --filter name=woltspace
curl -s http://localhost:4444/
```

---

## Known limitations after migration

1. **`/update` skill doesn't work** — platform code is baked into the image, not mounted. Update from host with `git pull && woltspace rebuild`.
2. **No live-reload of platform code** — changes to woltspace source require `woltspace rebuild --local`. This is intentional: wolts can no longer accidentally modify platform code.
3. **Dev mode changed** — `--local` builds from your local repo into the image (COPY, not mount). For iterative platform development, you'll rebuild more often but with better isolation.
4. **Deploy key mount removed** — if you were using SSH deploy keys for git push from inside the container, this needs a different approach (e.g., GH_PAT_TOKEN in .env).

---

## Quick reference: old → new

```
~/wolts                        → ~/.woltspace/wolts (or set WOLTS_DIR)
localhost:4444                 → localhost:7777
ENABLE_TUNNEL=false            → WOLTSPACE_PUBLIC_TUNNEL=false
woltspace start --dev          → woltspace start --local
woltspace rebuild --dev        → woltspace rebuild --local
woltspace restart              → woltspace stop && woltspace start
/update (inside container)     → git pull && woltspace rebuild (on host)
```
