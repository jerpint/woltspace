---
name: wolf
description: Set up and manage scheduled cron jobs. Use when the user wants to run something on a schedule — daily digests, weekly reviews, reminders, or any recurring task.
---

# Wolf — Cron & Scheduler 🐺

The wolf manages scheduled tasks. When a cron fires, the wolf sends an immediate notification and executes the action.

## Wolf as its own wolt

The wolf should be its own wolt with `"type": "wolf"` in its `wolt.json`. Before setting up crons:

1. **Check if a wolf-wolt exists:** Run:
   ```bash
   python3 -c "
   import sys; sys.path.insert(0, '/workspace/woltspace/container/lib')
   from wolts import get_active_creature, find_by_type
   wolf = get_active_creature('wolf')
   print(f'active wolf: {wolf}' if wolf else 'no active wolf')
   for w in find_by_type('wolf'): print(f'  found: {w[\"name\"]} at {w[\"dir\"]}')
   "
   ```

2. **If no wolf-wolt exists:** Ask the user what to name their wolf. Then create it:
   ```bash
   create-creature-wolt <name> wolf --role "Scheduler" --description "Runs the pack's routines on schedule"
   ```
   This creates the wolt directory, sets `type: wolf` in wolt.json, writes minimal identity files, and registers it as the active wolf in woltspace.json.

   After creation, customize the identity — write a proper `identity.md` in `/workspace/wolts/<name>/wolt/memory/identity.md` that reflects the wolf's personality. Then create the cron config at `/workspace/wolts/<name>/wolt/wolf.json`.

3. **If a wolf-wolt already exists:** Edit its `wolt/wolf.json` directly at `/workspace/wolts/<name>/wolt/wolf.json`.

**Important:** Do NOT put `wolf.json` in a rodent-wolt. The wolf is a separate creature.

## Setting up a cron

Create or edit `wolt/wolf.json` in the wolf-wolt's directory:

```json
{
  "crons": [
    {
      "name": "digest",
      "schedule": "0 6 * * *",
      "action": "script",
      "command": "node /workspace/woltspace/cron/digest.mjs",
      "notify": "digest time — fetching news and papers",
      "timezone": "America/Montreal"
    },
    {
      "name": "weekly-review",
      "schedule": "0 10 * * 1",
      "action": "session",
      "prompt": "Write a weekly review of what we shipped this week",
      "creature": "beaver",
      "notify": "weekly review firing up"
    },
    {
      "name": "cleanup-worktrees",
      "schedule": "0 0 * * 0",
      "action": "script",
      "command": "wt clean",
      "notify": "cleaning up stale worktrees"
    }
  ]
}
```

## Cron entry fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | yes | Unique identifier for this cron |
| `schedule` | yes | Cron expression: `minute hour day month weekday` |
| `action` | yes | `script`, `session`, or `skill` |
| `notify` | no | Message sent as 🐺 notification when cron fires |
| `timezone` | no | IANA timezone (default: system timezone) |
| `command` | script | Shell command to run |
| `prompt` | session/skill | Prompt for Claude Code session |
| `creature` | session/skill | `beaver` (default) or `raccoon` |
| `skill` | skill | Skill name to invoke |

## Cron expression format

```
┌───────── minute (0-59)
│ ┌─────── hour (0-23)
│ │ ┌───── day of month (1-31)
│ │ │ ┌─── month (1-12)
│ │ │ │ ┌─ day of week (0-6, 0=Sunday)
│ │ │ │ │
* * * * *
```

Examples:
- `0 6 * * *` — daily at 6am
- `0 10 * * 1` — Mondays at 10am
- `*/15 * * * *` — every 15 minutes
- `0 9,17 * * 1-5` — 9am and 5pm weekdays

## Action types

### `script` — run a shell command
```json
{ "action": "script", "command": "node /path/to/script.js" }
```

### `session` — spawn a Claude Code session
```json
{ "action": "session", "prompt": "do the thing", "creature": "beaver" }
```

### `skill` — invoke a Claude Code skill
```json
{ "action": "skill", "skill": "digest", "creature": "beaver" }
```

## CLI

```bash
# From inside the container:
python -m creatures.wolf --list         # Show registered crons + last run times
python -m creatures.wolf --once         # Fire any due crons now and exit
python -m creatures.wolf --fire NAME    # Fire a specific cron by name (ignores schedule — great for debugging)
python -m creatures.wolf                # Run as background service (auto-started by entrypoint)
```

## Default crons

Every wolt ships with a default `wolf.json` containing the **update checker** — a daily cron that checks if a woltspace update is available:

```json
{
  "crons": [
    {
      "name": "update-check",
      "schedule": "0 10 * * *",
      "action": "script",
      "command": "bash /workspace/woltspace/container/cron/check-update.sh",
      "timezone": "America/Montreal"
    }
  ]
}
```

The update checker uses `git ls-remote` (no LLM, no clone) to compare the stored version against remote `main`. It only sends a 🐺 notification when an update is actually found — completely silent otherwise. Version is tracked in `.state/woltspace-version`.

When adding new crons, add them to the existing `crons` array alongside the update checker.

## How it works

- Wolf reads `wolt/wolf.json` every 30 seconds
- When a cron matches the current time, wolf fires it (idempotent — won't double-fire within the same minute)
- Notifications go via 🐺 through the existing notify pipeline (sessionless notifications skip the reply footer)
- Last-run timestamps stored in `.state/wolf/{name}.last`
- Wolf starts automatically when `wolt/wolf.json` exists (entrypoint seeds a default if missing)
- To add a new cron, just edit wolf.json — wolf picks it up on next check
