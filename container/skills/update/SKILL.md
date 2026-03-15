---
name: update
description: "Update woltspace platform — review incoming changes, warn about breaking changes, and pull with user consent. Use when asked to update woltspace."
user_invocable: true
---

# Update Woltspace

You are the responsible updater. Your job is to protect the user — review what's coming, flag anything risky, and only pull with explicit consent.

`/workspace/woltspace` is volume-mounted from the host, so `git pull` updates files live — no rebuild needed.

## Step 1: Determine what's incoming

```bash
WOLT_DIR="${WOLT_DIR:-/workspace/wolts/${WOLT_NAME:-wolt}}"
BRANCH=$(cat "$WOLT_DIR/.state/woltspace-branch" 2>/dev/null || echo "main")

cd /workspace/woltspace
git fetch origin "$BRANCH"
```

Check if there's actually anything new:
```bash
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH")
```

If they match, tell the user they're already up to date and stop.

## Step 2: Review the changes

Look at what's incoming:
```bash
git log --oneline HEAD..origin/$BRANCH
git diff --stat HEAD..origin/$BRANCH
```

Read the actual diff for anything that looks like it could break existing behavior. Focus on:

- **Config/env changes** — new required env vars, renamed vars, changed defaults
- **Removed or renamed files** — skills, cron scripts, bot tools that wolts might depend on
- **CLAUDE.md changes** — new instructions that change how sessions behave
- **entrypoint.sh changes** — startup behavior, process management
- **woltspace CLI changes** — flag changes, new required args
- **Database/state format changes** — .state files, woltspace.json schema changes
- **Bot behavior changes** — tool renames, removed capabilities, changed routing

If a merge PR exists for the incoming commits, read it for context. Check if there's a changelog or migration notes.

## Step 3: Report to the user

Send a notify with your assessment. Structure it as:

**If safe (no breaking changes):**
> X commits incoming on [branch]. [1-2 sentence summary of what's new]. No breaking changes — safe to pull. Want me to update?

**If there are concerns:**
> X commits incoming on [branch]. [summary of what's new]. Heads up: [specific concern — e.g. "new OPENAI_API_KEY env var required for image gen" or "wolf skill renamed, existing wolf crons may need reconfiguring"]. Want me to proceed anyway?

Be specific about what might break. Don't just say "there are breaking changes" — say exactly what and what the user would need to do.

## Step 4: Pull (only after user says yes)

Do NOT pull until the user explicitly confirms. Then:

```bash
cd /workspace/woltspace && git pull origin "$BRANCH"

# Stamp version
NEW_VERSION=$(git rev-parse HEAD)
mkdir -p "$WOLT_DIR/.state"
echo "$NEW_VERSION" > "$WOLT_DIR/.state/woltspace-version"
echo "$BRANCH" > "$WOLT_DIR/.state/woltspace-branch"
```

## Step 5: Post-update report

After pulling:
```bash
git log --oneline ORIG_HEAD..HEAD
```

### Start new platform services

Some updates introduce new background services. Check and start them if they aren't already running:

**Vulture (session reaper)** — if `container/creatures/vulture.py` exists but no vulture process is running:
```bash
# Check if vulture is already running
if ! pgrep -f "creatures.vulture" > /dev/null 2>&1; then
  echo "starting vulture reaper (new in this update)..."
  cd /workspace/woltspace/container
  PYTHONPATH="/workspace/woltspace/container/lib:${PYTHONPATH:-}" \
    python -m creatures.vulture --once 2>/dev/null || true  # immediate first pass
  PYTHONPATH="/workspace/woltspace/container/lib:${PYTHONPATH:-}" \
    python -m creatures.vulture &
  disown
fi
```

Notify the user with: what was updated, the new version hash, and any action items (e.g. "bot behavior changed — restart the bot to pick it up").

## Notes

- `woltspace rebuild` is a **host-only** command — no docker CLI inside the container
- `server.js` runs with `--watch` so it picks up file changes automatically
- The Telegram bot does NOT auto-restart — if bot code changed, tell the user to restart manually
- If the update includes new env vars, tell the user to add them to `~/wolts/.env`
- If entrypoint.sh changed, a container restart may be needed — flag this
