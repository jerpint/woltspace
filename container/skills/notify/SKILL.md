---
name: notify
description: Push a message back to the user on Telegram or Slack — from inside a session.
---

# Notify — Push Messages to the User

Sessions can send messages back to the user at any time. Use this when you finish something worth reporting, hit a blocker that needs input, or find something the user should know about mid-task.

## Usage

```bash
# Default — auto-routes via session registry, falls back to Telegram
notify --stdin

# Explicit Slack — send to a specific Slack channel + thread
notify --slack CHANNEL THREAD_TS --stdin

# Explicit Telegram — send to a specific Telegram chat
notify --telegram CHAT_ID --stdin
```

Start the command, then pass the exact message through your execution tool's
stdin input. Never interpolate generated or user-controlled message text into a
shell command string: the shell would expand backticks, `$()`, quotes, and
other metacharacters before `notify` receives them.

The legacy `notify "static message"` form remains available for compatibility,
but do not use it for generated or user-controlled text.

## Explicit routing

When your session receives a message from Slack or Telegram, the prepended context includes the routing info you need:

```
[slack message from human, channel=C0123ABC, thread=1711234567.890123]: hey do the thing
Reply by passing message text on stdin to: notify --slack C0123ABC 1711234567.890123 --stdin
```

```
[telegram message from human, chat_id=98765432]: hey do the thing
Reply by passing message text on stdin to: notify --telegram 98765432 --stdin
```

**Use the exact command from the prepended context.** This ensures your reply goes to the right place — the specific Slack thread or Telegram chat the message came from.

## When to use it

- Task finished → tell the user what got built and where to find it
- Something interesting found mid-task → don't wait until the end
- Blocked and need input → ask directly instead of silently stalling
- Long-running work → check in periodically so the user isn't polling

## Examples

```bash
# Task done
notify --stdin

# Blocked
notify --stdin

# Explicit Slack reply
notify --slack C0123ABC 1711234567.890123 --stdin

# Explicit Telegram reply
notify --telegram 98765432 --stdin
```

## How it works

`notify` POSTs to `localhost:7777/notify` → server sends via Telegram/Slack API directly. Synchronous, no polling delay.

With explicit flags (`--slack`, `--telegram`), the server skips session registry lookup entirely and sends directly to the specified target. Without flags, it falls back to session-based routing, then Telegram default.
