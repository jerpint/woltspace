# Wolf 🐺 — Human Reference

> Quick reference for humans. For the machine-readable version, see SKILL.md.

## What is it?

A background Python service that runs scheduled tasks (crons). You define what and when in a JSON file, wolf handles the rest — timing, notifications, dispatching.

## Architecture

```
 wolf.json                    wolf.py                     actions
┌──────────────┐      ┌─────────────────────┐      ┌──────────────────┐
│ {             │      │                     │      │                  │
│   "crons": [ │─────▶│  read config (30s)  │─────▶│  script (shell)  │
│     { name,  │      │  match cron expr    │      │  session (claude) │
│       sched, │      │  check idempotency  │      │  skill (invoke)  │
│       action │      │  notify 🐺          │      │                  │
│     }        │      │  fire action        │      └──────────────────┘
│   ]          │      │                     │
│ }            │      └─────────────────────┘
└──────────────┘              │
                              │ last-run state
                              ▼
                      .state/wolf/{name}.last
```

**Loop:** every 30 seconds, wolf reads `wolf.json`, checks each cron against current time, skips if already fired this minute, sends a 🐺 notification, then runs the action.

## Config file

Lives at `wolt/wolf.json` in each wolt's directory:

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
    }
  ]
}
```

**Fields:** `name` (unique ID), `schedule` (cron expression), `action` (`script`/`session`/`skill`), `notify` (optional message), `timezone` (optional, IANA), `command`/`prompt`/`skill` (depends on action type), `creature` (for sessions — `beaver`/`raccoon`/`otter`).

## Cron expressions

```
┌───────── minute (0-59)
│ ┌─────── hour (0-23)
│ │ ┌───── day of month (1-31)
│ │ │ ┌─── month (1-12)
│ │ │ │ ┌─ day of week (0-6, 0=Sunday)
│ │ │ │ │
* * * * *

0 6 * * *       daily at 6am
0 10 * * 1      Mondays at 10am
*/15 * * * *    every 15 minutes
0 9,17 * * 1-5  9am and 5pm weekdays
```

## CLI — debugging & inspection

```bash
python -m creatures.wolf --list         # what's registered + when it last ran
python -m creatures.wolf --fire digest  # fire "digest" NOW, skip the schedule
python -m creatures.wolf --once         # fire everything that's due right now
python -m creatures.wolf                # run as background service
```

`--fire NAME` is the key one for debugging — triggers any cron immediately without waiting.

## Dog integration

The Telegram/Slack bot (dog 🐶) has three wolf-related tools:

- **`wolf_schedules`** — "what's scheduled?" → lists all crons + last run times
- **`fire_wolf`** — "run the digest now" → triggers a cron by name
- **`check_update`** — "is there an update?" → checks if woltspace has a newer version available (git ls-remote, no LLM needed)

Dog can also spawn a **wolf session** (`creature="wolf"`) to help users set up or edit their `wolf.json` interactively.

## Default update checker

Every wolt ships with a built-in `update-check` cron that runs daily at 10am. It compares your local woltspace version against remote `main` — only notifies if an update is found (silent otherwise). No LLM, no sessions, just `git ls-remote`.

If notified, ask a beaver or raccoon to handle the update — they'll evaluate the diff and explain what changed before proceeding.

## Key files

```
container/creatures/wolf.py           ← the scheduler service
container/skills/woltspace-wolf/SKILL.md        ← wolt-facing docs (loaded by Claude Code)
container/skills/woltspace-wolf/HUMANS.md       ← this file
wolt/wolf.json                        ← per-wolt schedule config
.state/wolf/{name}.last               ← last-run timestamps
```

## Auto-start

Wolf starts automatically when `wolt/wolf.json` exists — the entrypoint checks for the file and launches `wolf.py` as a background process. If the active wolt has no `wolf.json`, the entrypoint seeds one from the template (containing the update checker). No manual setup needed. Edit the JSON, wolf picks it up within 30 seconds.
