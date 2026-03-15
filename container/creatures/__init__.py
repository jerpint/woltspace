"""
Creature services — background and specialized role-based agents.

Each creature has a single clear role in the wolt colony:

  🐶 dog    — Lodge companion (loyal, constrained, always-on Telegram presence)
  🐺 wolf   — Cron & scheduler (runs the pack's routines on schedule)
  🕷️  spider — Headless browser (crawls, scrapes, watches the web)
  🐻 bear   — Safety & validation (guards the den, reviews outputs)
  🐼 panda  — Daily reminders & zen notifications (gentle, unhurried)

Active session creatures (dog/raccoon/beaver) live in bot/core.py.
These creatures are background services or specialized invocations.
"""
