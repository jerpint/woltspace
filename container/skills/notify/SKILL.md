---
name: notify
description: Push a message back to the user on Telegram or Slack — from inside a session.
---

# Notify — Push Messages to the User

Sessions can send messages back to the user at any time. Use this when you finish something worth reporting, hit a blocker that needs input, or find something the user should know about mid-task.

## Usage

```bash
# Default — auto-routes via session registry, falls back to Telegram
notify <<'WOLTSPACE_NOTIFY_7F3A91C2'
your message here
WOLTSPACE_NOTIFY_7F3A91C2

# Explicit Slack — send to a specific Slack channel + thread
notify --slack CHANNEL THREAD_TS <<'WOLTSPACE_NOTIFY_4D8E20B1'
your message here
WOLTSPACE_NOTIFY_4D8E20B1

# Explicit Telegram — send to a specific Telegram chat
notify --telegram CHAT_ID <<'WOLTSPACE_NOTIFY_A6C195E4'
your message here
WOLTSPACE_NOTIFY_A6C195E4
```

Use a fresh random suffix in the delimiter for every message, and quote the
opening delimiter exactly as shown. Put the closing delimiter on a line by
itself. A single-quoted heredoc passes its body literally: the shell does not
expand backticks, `$()`, variables, quotes, or other metacharacters.

`notify` accepts no message argument. This keeps message text out of the shell
command itself on every harness.

## Explicit routing

When your session receives a message from Slack or Telegram, the prepended context includes the routing info you need:

```
[slack message from human, channel=C0123ABC, thread=1711234567.890123]: hey do the thing
Reply by replacing YOUR_REPLY in this exact single-quoted heredoc, then run it:
notify --slack C0123ABC 1711234567.890123 <<'WOLTSPACE_NOTIFY_4D8E20B1'
YOUR_REPLY
WOLTSPACE_NOTIFY_4D8E20B1
```

```
[telegram message from human, chat_id=98765432]: hey do the thing
Reply by replacing YOUR_REPLY in this exact single-quoted heredoc, then run it:
notify --telegram 98765432 <<'WOLTSPACE_NOTIFY_A6C195E4'
YOUR_REPLY
WOLTSPACE_NOTIFY_A6C195E4
```

**Use the exact heredoc from the prepended context and replace only `YOUR_REPLY`.**
Woltspace generates a fresh delimiter every time. This ensures the message stays
literal and goes to the specific Slack thread or Telegram chat it came from;
you do not need to load this skill merely to reply.

## When to use it

- Task finished → tell the user what got built and where to find it
- Something interesting found mid-task → don't wait until the end
- Blocked and need input → ask directly instead of silently stalling
- Long-running work → check in periodically so the user isn't polling

## Examples

```bash
# Task done
notify <<'WOLTSPACE_NOTIFY_08D741BE'
playlist built — 12 tracks, Karkwa + kin. https://open.spotify.com/playlist/xyz
WOLTSPACE_NOTIFY_08D741BE

# Blocked
notify <<'WOLTSPACE_NOTIFY_B2A563F9'
hit a rate limit on Spotify API — should I retry in 30s or skip?
WOLTSPACE_NOTIFY_B2A563F9

# Explicit Slack reply
notify --slack C0123ABC 1711234567.890123 <<'WOLTSPACE_NOTIFY_94C8D130'
done — PR opened at github.com/...
WOLTSPACE_NOTIFY_94C8D130

# Explicit Telegram reply
notify --telegram 98765432 <<'WOLTSPACE_NOTIFY_E70A249C'
found the bug, fix pushed
WOLTSPACE_NOTIFY_E70A249C
```

## How it works

`notify` POSTs to `localhost:7777/notify` → server sends via Telegram/Slack API directly. Synchronous, no polling delay.

With explicit flags (`--slack`, `--telegram`), the server skips session registry lookup entirely and sends directly to the specified target. Without flags, it falls back to session-based routing, then Telegram default.
