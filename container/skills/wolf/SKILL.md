---
name: wolf
description: Set up and manage scheduled cron jobs. Use when the user wants to run something on a schedule — daily digests, weekly reviews, reminders, or any recurring task.
---

# Wolf — Cron & Scheduler 🐺

The wolf manages scheduled tasks. When a cron fires, the wolf sends an immediate notification and executes the action.

## How it works — at a glance

```
                        wolt/wolf.json
                        ┌─────────────────────────┐
                        │ { "crons": [             │
                        │   { name, schedule,      │
                        │     action, notify }     │
                        │ ] }                      │
                        └────────────┬────────────┘
                                     │
                                     │ reads every 30s
                                     ▼
                              ┌──────────────┐
                              │  🐺 wolf.py  │
                              │  (background) │
                              └──────┬───────┘
                                     │
                    ┌────────────────┼────────────────┐
                    │                │                │
               cron matches?    idempotent?      fire action
               (cron parser)   (last-run check)      │
                    │                │                │
                    │         .state/wolf/       ┌────┴────┐
                    │         {name}.last        │         │
                    │                       ┌────┴──┐  ┌──┴────┐
                    │                       │notify │  │action │
                    │                       │  🐺   │  │       │
                    │                       └───┬───┘  └───┬───┘
                    │                           │          │
                    │                      telegram/   ┌───┼───────┐
                    │                      slack       │   │       │
                    │                                  │   │       │
                    │                              script session skill
                    │                              (shell)(claude)(skill)
                    │
                    │
        ┌───────────┴───────────────────────────┐
        │  Dog integration (haiku bot)          │
        │                                       │
        │  wolf_schedules → check what's set up │
        │  fire_wolf      → trigger by name     │
        │  creature=wolf  → spawn wolf session  │
        │                   for interactive     │
        │                   cron setup          │
        └───────────────────────────────────────┘
```

**The loop:** wolf.json → wolf reads it → cron matches? → already fired this minute? → no → notify 🐺 → run action

**Dog knows about wolves:** `wolf_schedules` tool checks crons, `fire_wolf` triggers one on demand, and dog can spawn a wolf session (`creature="wolf"`) to help users configure their schedules interactively.

---

## Setting up a cron

Create or edit `wolt/wolf.json` in the wolt directory:

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
| `creature` | session/skill | `beaver` (default), `raccoon`, or `otter` |
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

## Files

```
container/creatures/wolf.py       — the scheduler (background service)
wolt/wolf.json                    — schedule config (per wolt)
.state/wolf/{name}.last           — last-run timestamps (idempotency)
container/skills/wolf/SKILL.md    — this file
```
