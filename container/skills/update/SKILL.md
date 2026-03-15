---
name: update
description: "Update woltspace platform by pulling latest changes. Use when asked to update woltspace."
user_invocable: true
---

# Update Woltspace

Pull latest changes and update version tracking. `/workspace/woltspace` is volume-mounted from the host, so files update live — no rebuild needed.

## Steps

```bash
# 1. Determine which branch to pull (dev mode tracks staging, otherwise main)
WOLT_DIR="${WOLT_DIR:-/workspace/wolts/${WOLT_NAME:-wolt}}"
BRANCH=$(cat "$WOLT_DIR/.state/woltspace-branch" 2>/dev/null || echo "main")

# 2. Pull latest
cd /workspace/woltspace && git pull origin "$BRANCH"

# 3. Stamp the new version so update-check cron knows we're current
NEW_VERSION=$(git rev-parse HEAD)
mkdir -p "$WOLT_DIR/.state"
echo "$NEW_VERSION" > "$WOLT_DIR/.state/woltspace-version"
echo "$BRANCH" > "$WOLT_DIR/.state/woltspace-branch"

echo "updated to $(echo $NEW_VERSION | cut -c1-7) ($BRANCH)"
```

## What to report

After pulling, run `git log --oneline ORIG_HEAD..HEAD` to show what changed. Summarize the changes in a notify message: what was fixed or added, the new version hash.

## Notes

- `woltspace rebuild` is a **host-only** command — no docker CLI inside the container
- `server.js` runs with `--watch` so it picks up file changes automatically
- The Telegram bot does NOT auto-restart — if bot behavior changed, tell the user to restart the bot manually
