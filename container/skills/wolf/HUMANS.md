# Wolf 🐺 — Human Reference

> Quick reference for humans. For the wolt-facing version, see SKILL.md.

## What is it?

The lodge's cron scheduler. Each wolt keeps its schedules in its own
`wolt/wolf.json`; the wolf reads all of them, and when one is due it starts a
session for that wolt with the cron's message as the prompt and sends a 🐺
notification. It is a supervised connector: it starts with the lodge, restarts
if it dies, and shows up in `woltspace status`.

**Time is the lodge machine's local time.** A cron written `0 9 * * *` fires at
09:00 on the clock of the machine the lodge runs on.

## Architecture

```
 wolts/*/wolt/wolf.json          creatures/wolf.py              lodge
┌──────────────────────┐      ┌────────────────────────┐     ┌──────────────────┐
│ {"crons": [          │─────▶│ every 30s: load, check │────▶│ start a session  │
│   {name, schedule|at,│      │ due (lib/wolfcore.py), │     │ for the owning   │
│    prompt, notify?,  │      │ stamp, dispatch, ping? │     │ wolt; 🐺 ping on │
│                      │      │                        │     │ notify's channel │
│    catch_up?}        │      └────────────────────────┘     └──────────────────┘
│ ]}                   │                 │
└──────────────────────┘                 ▼
        ▲                     .space/wolf/<wolt>/<name>.last   last-run stamps
        │                     .space/wolf/jobs.jsonl           job journal
  woltspace wolf ... ──▶ /wolf/crons API (validates, locks, writes)
```

`container/lib/wolfcore.py` is the one place the rules live — parsing, when a
cron is due, when it runs next, locked writes of `wolf.json`. The scheduler and
the lodge API both use it, so what the API accepts is exactly what the wolf fires.

## CLI

```bash
woltspace wolf list [--wolt W] [--json]
woltspace wolf add --wolt W --cron '0 9 * * 1-5' <<'WOLF_MSG'
Write the standup notes.
WOLF_MSG
woltspace wolf add --wolt W --at 2026-03-22T14:30 --message 'check the deploy'
woltspace wolf set W NAME [--cron EXPR | --at TIME] [--message TEXT|-] [--notify telegram|slack|''] [--move-to WOLT]
woltspace wolf rm W NAME
woltspace wolf run W NAME          # run now; schedule untouched, journaled "manual"
woltspace wolf runs [--wolt W] [--limit N]
```

`add --dry-run` shows the entry and the resulting file without writing. Inside a
wolt session `--wolt` defaults to that wolt.

## Rules

- Five-field cron: `*`, lists, ranges, `*/n`, `n/n`, `a-b/n`; day of week 0 or 7 = Sunday. Out-of-range values are rejected on write.
- Day-of-month and day-of-week are **ANDed** (classic cron ORs them): `0 9 13 * 5` is only Friday the 13th.
- Fires once per matching minute. After a restart it fires each cron's most recent missed run within 24 hours, once — unless the cron has `"catch_up": false`.
- One-offs (`at`) fire once their time passes, then are removed from `wolf.json`.
- Names are unique per wolt. An entry the wolf cannot read (hand-edited typo) is logged and skipped; the others keep firing.

## Hand-editing

`wolt/wolf.json` is still just a file. Edit it and the wolf picks it up within
30 seconds. The CLI and API take the same lock the wolf does, write atomically,
and keep any extra keys you added.

## Lodge API

| Route | Does |
|-------|------|
| `GET /wolf/crons` | every cron with prompt, next/last run, the lodge's zone, and the ping `channels` that can deliver |
| `POST /wolf/crons[?dry_run=1]` | add (`{wolt, schedule\|at, prompt, name?, notify?, catch_up?}`) |
| `PUT /wolf/crons/<wolt>/<name>` | partial update; `wolt` in the body moves it |
| `DELETE /wolf/crons/<wolt>/<name>` | remove |
| `POST /wolf/crons/<wolt>/<name>/fire` | run now |
| `GET /wolf/schedules`, `GET /wolf/fires` | read-only views (schedules omit prompts) |

Errors: `400 {error, field}` for validation, `404` unknown wolt or cron, `409`
name taken or an unreadable `wolf.json`.

## Dog integration

The Telegram/Slack dog 🐶 has `wolf_schedules` (what's scheduled), `fire_wolf`
(run one by name) and `wolf_jobs` (recent journal).

## Key files

```
container/creatures/wolf.py      ← the scheduler loop
container/lib/wolfcore.py        ← shared cron logic
server/app.py  (# --- Wolf)      ← /wolf/* routes
src/woltspace/cli.py             ← woltspace wolf
wolt/wolf.json                   ← per-wolt schedule
.space/wolf/                     ← last-run stamps + jobs.jsonl
```
