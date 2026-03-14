"""
🐼 Panda — Daily Reminders & Zen Notifications

The panda is gentle and unhurried. It surfaces what matters, when it matters.
No noise — just the signal worth surfacing.

Role:
  - Daily check-ins: "here's what's on for today"
  - Reminders: time-based, context-aware, not spammy
  - Zen-mode notifications: calm, brief, never urgent
  - Works alongside wolf (wolf schedules; panda delivers the message)

Design:
  - Panda is a notification composer, not a scheduler
  - Given context (today's tasks, open threads, calendar) → writes one gentle message
  - Low-model (haiku) — the message should be short and warm, not elaborate
  - Sends via notify (Telegram) — always one message, never a list of bullets
  - Runs once per trigger (morning, evening, or on-demand)

Entry point:
  - Called by wolf on schedule: `python -m creatures.panda --mode morning`
  - Or triggered directly: `python -m creatures.panda --remind "standup in 10m"`
  - Modes: morning (what's up today), evening (what got done), nudge (one reminder)

Voice:
  - Calm, brief. "hey — standup in ten. you've got two open threads worth closing first."
  - Never formal. Never a list. Never "I noticed that..."
  - If there's nothing to say, says nothing (no "all clear!" filler)

TODO (implementation):
  - Define context inputs (context.md snapshot, open thread list, calendar if available)
  - Write panda's prompt (haiku, very low token budget, warm tone)
  - Implement morning/evening/nudge modes
  - Hook into wolf's schedule config as a cron job
  - Test: panda output is always under 3 sentences, never uses bullet points
"""

# Placeholder — not yet implemented


def compose(mode: str = "morning", context: dict = None) -> str:
    raise NotImplementedError("panda is not yet implemented")
