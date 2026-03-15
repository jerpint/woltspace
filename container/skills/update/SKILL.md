---
name: update
description: "Update woltspace platform by pulling latest main. Use when asked to update woltspace."
user_invocable: true
---

# Update Woltspace

Pull latest `main` and update version tracking. No rebuild needed — `/workspace/woltspace` is volume-mounted from the host, so files update live.

## Steps

```bash
# 1. Pull latest main
cd /workspace/woltspace && git pull origin main

# 2. Stamp the new version so update-check cron knows we're current
NEW_VERSION=$(cd /workspace/woltspace && git rev-parse HEAD)
WOLT_DIR="${WOLT_DIR:-/workspace/wolts/${WOLT_NAME:-wolt}}"
mkdir -p "$WOLT_DIR/.state"
echo "$NEW_VERSION" > "$WOLT_DIR/.state/woltspace-version"
echo "main" > "$WOLT_DIR/.state/woltspace-branch"

echo "updated to $(echo $NEW_VERSION | cut -c1-7)"
```

## What to report

After pulling, run `git log --oneline ORIG_HEAD..HEAD` to show what changed. Summarize the changes in a notify message: what was fixed or added, the new version hash.

## Notes

- `woltspace rebuild` is a **host-only** command — no docker CLI inside the container
- `server.js` runs with `--watch` so it picks up file changes automatically
- The Telegram bot does NOT auto-restart — if bot behavior changed, tell the user to restart the bot manually
