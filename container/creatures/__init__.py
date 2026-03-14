"""
Creature services — background and specialized role-based agents.

Each creature has a single clear role in the wolt colony:

  🐕 dog    — Telegram companion (loyal, constrained, fetches what you need)
  🐺 wolf   — Cron & scheduler (runs the pack's routines on schedule)
  🕷️  spider — Headless browser (crawls, scrapes, watches the web)
  🐻 bear   — Safety & validation (guards the den, reviews outputs)
  🐼 panda  — Daily reminders & zen notifications (gentle, unhurried)

Active session creatures (raccoon/beaver/otter) live in bot/core.py.
These creatures are background services or specialized invocations.
"""
