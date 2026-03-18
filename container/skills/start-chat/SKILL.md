---
name: start-chat
description: Bootstrap a session with the right context for its adapter and wolt. Used as the opening prompt when spawning sessions.
---

# Start Chat

You are starting a new session. Your arguments tell you how you got here and who you are.

## Arguments

You were invoked as: `/start-chat <mode> <wolt-name>`

- **mode** — how this session was started: `lodge`, `telegram`, or `slack`
- **wolt-name** — the wolt you are (e.g. `neowolt`, `UXwolt`, `blabo`)

## What to do

1. **Read your mode file** at `/workspace/woltspace/container/skills/start-chat/modes/<mode>.md` — this tells you how to behave in this session (notification style, viewport usage, constraints).

2. **Read your identity** from your wolt's memory directory at `/workspace/wolts/<wolt-name>/wolt/memory/`. Start with `identity.md` if it exists, then `context.md` and `learnings.md`. These are your boot files — they tell you who you are, what you're working on, and what you've learned.

3. **Read your CLAUDE.md** at `/workspace/wolts/<wolt-name>/CLAUDE.md` if it exists — this has project-specific instructions.

4. **Greet the user** according to your mode. Keep it short — one line is fine.

Do NOT summarize what you read. Just absorb the context and be ready to work.
