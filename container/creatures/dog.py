"""
🐕 Dog — Telegram Companion

The dog is the face of the lodge on Telegram. Loyal, constrained, fetches what you need.
Based on Fuji.

Role:
  - Primary Telegram presence — the wolt's voice on mobile
  - Runs tools, answers questions, dispatches beavers and raccoons
  - Short memory, focused context — doesn't carry everything, just the current ask
  - Knows when to escalate (spawn a session) vs handle inline

Relationship to otter/bot:
  - Currently the Telegram adapter runs as "otter" (haiku, general)
  - Dog is a *named identity* layered on top — same model, different persona
  - Dog knows it's dog: loyal, direct, slightly terse, never overthinks
  - Over time: dog gets its own memory slice, its own system prompt tuning

Design:
  - This file is the identity layer — not the adapter itself
  - `dog_prompt()` → returns dog-specific system prompt additions
  - `dog_persona` → name, emoji, voice guidelines
  - Imported by telegram_adapter.py to override the generic "otter" persona

Entry point:
  - Not a standalone service — wired into telegram_adapter.py
  - `from creatures.dog import dog_prompt` → injects into build_system_prompt()
  - Or: BOT_CREATURE=dog env var triggers dog identity in the adapter

TODO (implementation):
  - Write dog's voice guidelines (terse, loyal, "fetches things", Fuji energy)
  - Wire into telegram_adapter.py via BOT_CREATURE env var
  - Dog-specific ack messages (not beaver quotes — dog phrases)
  - Dog memory: shorter trim window, different context focus
  - Consider: dog has a few fixed commands (/fetch, /status, /where)
"""

# Placeholder — not yet implemented

DOG_NAME = "fuji"
DOG_EMOJI = "🐕"


def dog_prompt() -> str:
    raise NotImplementedError("dog persona not yet wired in")
