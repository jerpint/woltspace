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

## Constraints

**NEVER restart, kill, or modify server.js (port 7777)** — it runs the tunnel, split view, and all session routing. Restarting it breaks everything for everyone. If something seems wrong with the server, notify the developer and stop.

**You can ONLY edit files inside your wolt directory.** Never edit, create, or delete files in:
- `/workspace/woltspace/` — this is the platform code. Editing it breaks updates.
- Other wolts' directories
- System files outside your wolt

All code you write goes in `wolt/projects/` (for code projects) or `wolt/site/` (for static pages). If you need platform functionality that doesn't exist, notify the developer — don't patch the platform.
