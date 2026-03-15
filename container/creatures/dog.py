"""
🐶 Dog — Lodge Companion

The dog is the face of the lodge on Telegram. Loyal, constrained, fetches what you need.
Based on Fuji. Now the default lodge creature (replacing otter).

Role:
  - Primary Telegram presence — the wolt's voice on mobile
  - Runs tools, answers questions, dispatches beavers and raccoons
  - Short memory, focused context — doesn't carry everything, just the current ask
  - Knows when to escalate (spawn a session) vs handle inline

Status: ACTIVE
  - Dog identity is now live in core.py system prompt and telegram_adapter.py
  - Bot responses prefixed with 🐶
  - Voice messages acked with 🐶

Design:
  - Identity is defined in core.py build_system_prompt() — dog is the default lodge creature
  - telegram_adapter.py prepends 🐶 to all responses (deterministic, invisible to LLM)
  - Over time: dog gets its own memory slice, voice tuning, and commands

Future:
  - Dog-specific ack messages (not beaver quotes — dog phrases)
  - Dog memory: shorter trim window, different context focus
  - Fixed commands (/fetch, /status, /where)
"""

DOG_NAME = "fuji"
DOG_EMOJI = "🐶"
