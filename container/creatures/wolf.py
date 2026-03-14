"""
🐺 Wolf — Cron & Scheduler

The wolf runs the pack's routines. It fires tasks on schedule, tracks cadence,
and wakes up the right creature at the right time.

Role:
  - Owns all scheduled work (crons, recurring tasks, reminders)
  - Replaces the hardcoded digest.mjs cron — wolf dispatches any cron task
  - Lightweight (haiku-class model) — fast, always-on, minimal cost
  - Does not do the work itself; fires other creatures (beaver, spider, panda)

Design:
  - Reads a schedule config (wolt.json or wolf.yaml — TBD)
  - Each cron entry: cron expression + prompt + creature to dispatch
  - Persists last-run timestamps to avoid double-fires on restart
  - Notifies on failures, not on success (silence = healthy)

Entry point:
  - Long-running service: `python -m creatures.wolf`
  - Or managed by the existing server cron mechanism

TODO (implementation):
  - Define schedule config format
  - Build the run loop (APScheduler or simple asyncio)
  - Replace digest.mjs cron in server.js / entrypoint.sh
  - Hook into session registry so wolf-dispatched sessions are labeled
  - Health check endpoint (GET /creatures/wolf/status)
"""

# Placeholder — not yet implemented


def run():
    raise NotImplementedError("wolf is not yet implemented")
