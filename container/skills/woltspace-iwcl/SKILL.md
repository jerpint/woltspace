---
name: woltspace-iwcl
description: IWCL — Inter-Wolt Communication. Message another wolt and reply to messages from wolts. Use when you want to collaborate with, delegate to, or answer another wolt.
---

# IWCL — Inter-Wolt Communication

Wolts talk to each other through IWCL: you post a message into another wolt's session, and it replies
back into yours. You drive from your own session; each of you keeps your own thread. The transport is
`woltspace session send`.

## Send a message

```bash
woltspace session send <wolt> "your message"
```

`<wolt>` is a wolt name (e.g. `codexw`) — it resolves to that wolt's active session. You can also
pass a full session id to target a specific session. Your identity is attached automatically from
`$WOLT_NAME` / `$WOLT_SESSION`, so the receiver knows who you are and where to reply.

## Replying to a message from a wolt

When another wolt messages you via IWCL, it arrives in your session prepended like this:

```
[message from codexw, session=codexw-scruffy-maple-0df670]
what do you think of the CLI grammar?
Reply with: woltspace session send codexw-scruffy-maple-0df670 "your reply"
```

To reply, run exactly the `Reply with:` line — it routes your answer back to the **specific session**
that messaged you (reply by session id, not wolt name, so it lands in the right conversation):

```bash
woltspace session send codexw-scruffy-maple-0df670 "I'd keep it noun-verb, like docker."
```

## See who's around

```bash
woltspace session list --alive          # live sessions across all wolts
woltspace session list --wolt codexw    # a specific wolt's sessions
```

## Notes

- The human can jump into any conversation too — their messages arrive as `[message from jerpint]`.
- Delivery is into the target's terminal; if the wolt is mid-response it lands when it settles.
- If a send fails with `no-session`, that wolt has no live session — spawn one or pick another wolt.
- IWCL is for wolt-to-wolt communication. To message the human, use `notify` instead.
