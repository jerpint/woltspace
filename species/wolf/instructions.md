# Wolf — Cron Scheduler

The wolf is a background daemon. It runs continuously, watches `wolt/wolf.json` for cron entries, and fires jobs on schedule.

## Behavior

- Read cron config from the active wolf-wolt's `wolt/wolf.json`
- Fire jobs as named tmux sessions for visibility
- Send notifications on fire, success, and failure
- Log state to `.state/wolf/`

## Constraints

- Singleton — only one wolf active per woltspace
- The wolf is a service, not a personality. Functional identity only.
- Users never talk to the wolf directly. Rodent sessions configure it by editing wolf.json.
- Each cron entry has: name, schedule (cron expression), action (script/session/skill), optional notification message.

## Configuration

Rodent sessions with the wolf scheduling skill can:
- Add/remove/edit cron entries in wolf.json
- Check what's scheduled (wolf_schedules)
- Trigger a cron immediately (fire_wolf)
- View job history (wolf_jobs)
