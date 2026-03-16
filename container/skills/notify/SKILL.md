---
name: notify
description: Push a message back to the user on Telegram or Slack — from inside a session.
---

# Notify — Push Messages to the User

Sessions can send messages back to the user at any time. Use this when you finish something worth reporting, hit a blocker that needs input, or find something the user should know about mid-task.

## Usage

```bash
notify "your message here"
```

That's it. `notify` is on PATH in every session.

## When to use it

- Task finished → tell the user what got built and where to find it
- Something interesting found mid-task → don't wait until the end
- Blocked and need input → ask directly instead of silently stalling
- Long-running work → check in periodically so the user isn't polling

## Routing

- Session started from **Telegram** → message goes to that Telegram chat
- Session started from **Slack** → message goes to that Slack thread
- Interactive/manual session → falls back to Telegram

No configuration needed. Routing is automatic based on how the session was started.

## Examples

```bash
# Task done
notify "playlist built — 12 tracks, Karkwa + kin. https://open.spotify.com/playlist/xyz"

# Blocked
notify "hit a rate limit on Spotify API — should I retry in 30s or skip?"

# Mid-task update
notify "found 3 relevant papers, summarizing now"

# Something interesting
notify "HN thread from 2019 has exactly what you were asking about — dropping link in viewport"
```

## How it works

`notify` POSTs to `localhost:7777/notify` → server reads session routing → calls Telegram/Slack API directly. Synchronous, no polling delay.
