"""
Creature services — background and specialized role-based agents.

The implemented background services are:

  🐺 wolf    — cron and scheduler
  🦅 vulture — session reconciliation and reaping

Session-creature identity lives in bot/core.py; this package contains only
actual background-service implementations.
"""
