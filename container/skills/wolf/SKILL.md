---
name: wolf
description: Set up and manage scheduled cron jobs. Use when the user wants to run something on a schedule — daily digests, weekly reviews, reminders, or any recurring task.
---

# Wolf — Cron Scheduler

The wolf is a background scheduler. Each wolt owns its own crons in `wolt/wolf.json`. The wolf scans all wolts, fires crons on schedule, and spawns sessions for the owning wolt.

## Your wolf.json

Your cron file lives at your own `wolt/wolf.json`:

```json
{
  "crons": [
    {
      "name": "morning-playlist",
      "schedule": "0 10 * * *",
      "prompt": "/music",
      "notify": "morning playlist time"
    }
  ]
}
```

To add a cron, edit your `wolt/wolf.json` directly. The wolf picks up changes every 30 seconds.

## Cron entry fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | yes | Unique identifier for this cron |
| `schedule` | for recurring | Cron expression: `minute hour day month weekday` |
| `at` | for one-offs | ISO timestamp (e.g. `2026-03-22T10:30`) — fires once, then auto-deletes |
| `prompt` | yes | What the session runs — can be a `/skill` or plain text |
| `notify` | no | Message sent as notification when cron fires |

Every cron needs either `schedule` (recurring) or `at` (one-off), not both.

## Cron expression format

```
minute hour day month weekday
  0     10    *   *     *       = daily at 10:00 UTC
  0     10    *   *     1       = Mondays at 10:00 UTC
 */15    *    *   *     *       = every 15 minutes
  0    9,17   *   *    1-5      = 9am and 5pm weekdays
```

**Important:** The server runs on UTC. Convert your local time accordingly.

## One-off crons

Use `"at"` instead of `"schedule"` for tasks that should fire once:

```json
{
  "name": "quick-check",
  "at": "2026-03-22T14:30",
  "prompt": "check if the deploy went through",
  "notify": "running deploy check"
}
```

The wolf auto-deletes one-off entries from your wolf.json after they fire.

## Examples

**Daily digest at 6am Montreal (10:00 UTC):**
```json
{ "name": "digest", "schedule": "0 10 * * *", "prompt": "/digest", "notify": "digest time" }
```

**Weekly review on Mondays:**
```json
{ "name": "weekly-review", "schedule": "0 14 * * 1", "prompt": "Write a weekly review of what we shipped", "notify": "weekly review time" }
```

**One-off reminder in 30 minutes:**
```json
{ "name": "reminder", "at": "2026-03-22T11:00", "prompt": "Remind jerpint to review the PR", "notify": "reminder" }
```

## How it works

- Wolf scans `wolts/*/wolt/wolf.json` every 30 seconds
- When a cron matches, wolf spawns a session for the owning wolt via `/sessions/new/lodge`
- A notification is sent: `🐺 *Howl* — 🦫 nunu has been notified: "morning playlist time"`
- Won't double-fire within the same minute (idempotent)
- Last-run timestamps and the job journal are lodge-global, in `.space/wolf/`
- Read them without shelling in: `GET /wolf/schedules` and `GET /wolf/fires`

## CLI (for debugging)

```bash
python -m creatures.wolf --list         # Show all crons + last run times
python -m creatures.wolf --once         # Fire any due crons now and exit
python -m creatures.wolf --fire NAME    # Fire a specific cron by name (ignores schedule)
python -m creatures.wolf                # Run as background service
```
