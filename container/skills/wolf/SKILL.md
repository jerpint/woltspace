---
name: wolf
description: Set up and manage scheduled cron jobs. Use when the user wants to run something on a schedule — daily digests, weekly reviews, reminders, or any recurring task.
---

# Wolf — Cron Scheduler

The wolf is the lodge's scheduler. Each wolt owns its crons in its own
`wolt/wolf.json`; the wolf scans every wolt every 30 seconds, and when a cron is
due it starts a session for the owning wolt with the cron's message as the
prompt, then sends a 🐺 notification.

Manage crons with `woltspace wolf`. It talks to the lodge, which validates
everything before writing `wolf.json`, so a mistake comes back as an error
instead of a cron that silently never fires.

## Time zone

**The wolf fires in the lodge machine's local time.** `0 9 * * *` means 09:00 on
the clock of the machine the lodge runs on — no UTC conversion. `woltspace wolf
list` prints the zone it is using, and every time it shows is in that zone.

## Add

The message (what the session is told to do) is read from stdin. Use a
single-quoted heredoc so the shell leaves it alone:

```bash
woltspace wolf add --cron '0 9 * * 1-5' --notify telegram <<'WOLF_MSG'
Write today's standup notes from yesterday's commits.
WOLF_MSG
```

One-off, fires once at a lodge-local time and then removes itself:

```bash
woltspace wolf add --at 2026-03-22T14:30 <<'WOLF_MSG'
Check whether the deploy went through and tell jerpint.
WOLF_MSG
```

- `--wolt` defaults to you (`$WOLTSPACE_WOLT_NAME`); pass it to schedule for another wolt.
- `--name` defaults to the message's first few words (`write-today-s-standup`), made unique in that wolt.
- `--notify telegram|slack` also pings your human there when it runs ("🐺 <wolt> woke up: <first line>" + session link). Leave it out and the wolf is quiet; the run still shows in `woltspace wolf runs` and on the lodge's wolves page. Slack needs `SLACK_NOTIFY_CHANNEL` set.
- `--message TEXT` instead of stdin for a one-liner.
- `--dry-run` shows the entry and the resulting file, writes nothing.
- `--json` on any command for machine-readable output.

## List, change, remove, run

```bash
woltspace wolf list [--wolt W] [--json]      # soonest first: schedule, next run, last run
woltspace wolf set <wolt> <name> --cron '30 7 * * *'
woltspace wolf set <wolt> <name> --at 2026-03-23T10:00      # switch to a one-off
woltspace wolf set <wolt> <name> --message - <<'WOLF_MSG'   # new message from stdin
New instructions.
WOLF_MSG
woltspace wolf set <wolt> <name> --notify ''                 # stop pinging (telegram|slack to turn it on)
woltspace wolf set <wolt> <name> --move-to <other-wolt>
woltspace wolf rm  <wolt> <name>
woltspace wolf run <wolt> <name>                             # run now; schedule untouched
woltspace wolf runs [--wolt W] [--limit N]                   # recent fires, newest first
```

Cron names are unique per wolt; two wolts can each have a `digest`.

## Cron expressions

```
minute hour day-of-month month day-of-week
  0     9        *         *       *        daily at 09:00
  0     9        *         *      1-5       weekdays at 09:00
 */15   *        *         *       *        every 15 minutes
  0   9,17       *         *       1        Mondays at 09:00 and 17:00
  0     9       13         *       5        Friday the 13th only
```

- `*`, lists `1,3,5`, ranges `1-5`, steps `*/15`, `5/10`, `1-10/2`. Day of week: 0 or 7 = Sunday.
- Numbers only (no `MON`/`JAN`), and out-of-range values (`61 * * * *`) are rejected.
- When both day-of-month and day-of-week are set, **both must match** (unlike classic cron, which fires on either).
- A schedule must fire at least once within a year.

## When crons fire

- The wolf checks every 30 seconds and fires a cron once in the minute it matches.
- If the wolf was down, on start it fires each recurring cron's most recent missed run within the last 24 hours — once. Set `"catch_up": false` on a cron to skip that.
- A one-off whose time has passed fires as soon as the wolf sees it, then is removed from `wolf.json`.
- Each fire and each manual run is journaled; see `woltspace wolf runs`.

## wolf.json

`woltspace wolf` edits this file for you; hand-editing it still works, and the
wolf picks up changes within 30 seconds. An entry the wolf cannot read is logged
and skipped, never fatal — but only the CLI checks your work before it lands.

```json
{
  "crons": [
    {
      "name": "standup",
      "schedule": "0 9 * * 1-5",
      "prompt": "Write today's standup notes from yesterday's commits.",
      "notify": "telegram"
    },
    {
      "name": "deploy-check",
      "at": "2026-03-22T14:30",
      "prompt": "Check whether the deploy went through.",
      "catch_up": false
    }
  ]
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `name` | yes | Unique within this wolt: letters, digits, `-`, `_` |
| `schedule` | recurring | Cron expression, lodge local time |
| `at` | one-off | `YYYY-MM-DDTHH:MM`, lodge local time — fires once, then is removed |
| `prompt` | yes | What the session is told — plain text or a `/skill` |
| `notify` | no | `telegram` or `slack`: ping your human there after each run. Missing = quiet. (Older free text still means telegram.) |
| `catch_up` | no | `false` = don't fire a run missed while the wolf was down |

Exactly one of `schedule` or `at`.

## Where state lives

- Schedules: each wolt's own `wolt/wolf.json`
- Last-run stamps and the job journal are lodge-global, in `.space/wolf/`
  (`<wolt>/<name>.last`, `jobs.jsonl`)
- Lodge API behind the CLI: `GET/POST /wolf/crons`, `PUT/DELETE /wolf/crons/<wolt>/<name>`,
  `POST /wolf/crons/<wolt>/<name>/fire`; read-only views: `GET /wolf/schedules`, `GET /wolf/fires`
