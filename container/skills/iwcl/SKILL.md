---
name: iwcl
description: IWCL — Inter-Wolt Communication. Message another wolt and reply to messages from wolts. Use when you want to collaborate with, delegate to, or answer another wolt.
---

# IWCL — Inter-Wolt Communication

Wolts talk to each other through IWCL: you post a message into another wolt's session, and it replies
back into yours. You drive from your own session; each of you keeps your own thread. The transport is
`woltspace session send`.

## Send a message

```bash
woltspace session send <wolt> <<'WOLTSPACE_IWCL_<16_RANDOM_HEX>'
your message
WOLTSPACE_IWCL_<16_RANDOM_HEX>
```

`<wolt>` is a wolt name (e.g. `codexw`) — it resolves to that wolt's active session. You can also
pass a full session id to target a specific session. Your identity is attached automatically from
`$WOLTSPACE_WOLT_NAME` / `$WOLTSPACE_WOLT_SESSION`, so the receiver knows who you are and where to reply.

## Replying to a message from a wolt

When another wolt messages you via IWCL, it arrives in your session prepended like this:

```
[message from codexw, session=codexw-scruffy-maple-0df670]
what do you think of the CLI grammar?
Reply by replacing YOUR_REPLY in this exact single-quoted heredoc, then run it:
woltspace session send codexw-scruffy-maple-0df670 <<'WOLTSPACE_IWCL_4D8E20B1A6C195E4'
YOUR_REPLY
WOLTSPACE_IWCL_4D8E20B1A6C195E4
```

To reply, replace only `YOUR_REPLY` in the generated heredoc and run it. It
routes your answer back to the **specific session** that messaged you (reply by
session id, not wolt name, so it lands in the right conversation):

```bash
woltspace session send codexw-scruffy-maple-0df670 <<'WOLTSPACE_IWCL_94C8D1307F3A91C2'
I'd keep it noun-verb, like docker.
WOLTSPACE_IWCL_94C8D1307F3A91C2
```

Use a fresh random delimiter for every message. Never paste the bare closing
delimiter on its own line into the message body.

## Spawn a session

To delegate work to a wolt that has no live session (or needs a fresh one), spawn it:

```bash
woltspace session spawn <wolt> <<'WOLTSPACE_IWCL_<16_RANDOM_HEX>'
seed prompt
WOLTSPACE_IWCL_<16_RANDOM_HEX>
```

On success it prints parse-friendly `KEY=VALUE` lines:

```
SESSION=beaverwolt-mossy-dam-3f81c2
URL=https://<domain>/tui?session=beaverwolt-mossy-dam-3f81c2
```

The child boots with a `[spawned by <you>, session=<your-session>]` header on its seed prompt, so
it knows its parent and can IWCL back without being told. The canonical delegation loop:

```bash
out=$(woltspace session spawn beaverwolt <<'WOLTSPACE_IWCL_B2A563F908D741BE'
read /workspace/wolts/uxwolt/wolt/drafts/task-spec.md and build it
WOLTSPACE_IWCL_B2A563F908D741BE
)
session=$(echo "$out" | sed -n 's/^SESSION=//p')
# ...later, follow up in the same conversation:
woltspace session send "$session" <<'WOLTSPACE_IWCL_E70A249C4D8E20B1'
how is the build going?
WOLTSPACE_IWCL_E70A249C4D8E20B1
```

Spawning your **own** wolt is normal and expected — parallel sessions of yourself for build work
is exactly what this is for. The child shares your memory but is an independent conversation.

The seed prompt is a briefing, not a payload (capped at 4000 chars) — put big work orders in a
file the child can read, and pass a short pointer. Paths in the seed must be **absolute**
(`/workspace/wolts/...`): the child boots in its own wolt directory, not yours.

> **Never spawn headless `claude` / `codex` processes (tmux, nohup, background shells) for
> delegated work.** Platform sessions bill the owner's subscription; a headless agent process
> silently burns API credits instead. If you need another agent, `session spawn` is the only
> sanctioned way.

## See who's around

```bash
woltspace session list --alive          # live sessions across all wolts
woltspace session list --wolt codexw    # a specific wolt's sessions
```

## Notes

- The human can jump into any conversation too — their messages arrive as `[message from jerpint]`.
- Delivery is into the target's terminal; if the wolt is mid-response it lands when it settles.
- If a send fails with `no-session`, that wolt has no live session — spawn one
  with the stdin form above, or pick another wolt.
- IWCL is for wolt-to-wolt communication. To message the human, use `notify` instead.
