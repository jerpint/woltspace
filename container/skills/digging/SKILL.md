---
name: digging
description: Use owner-approved SSH access to inspect or set up a remote machine, bootstrap a Woltspace colony, and leave a durable local handoff. Use when a human asks a wolt to SSH into, dig into, configure, or prepare another machine. Do not use for Wire pairing or cross-colony IWCL.
---

# Dig into an owned machine

Digging means temporarily using the owner's existing SSH access. The wolt stays
resident in its home colony: connect, do the authorized work, leave a local
handoff for future wolts on that machine, disconnect, and report home.

## Find or create the approval

Run `woltspace dig list --json`. Use an existing grant only when its wolt and
destination match the human's request.

If none matches, an explicit instruction such as “dig into `newbox` and set up
the colony” authorizes creating that local grant for the current wolt when
`newbox` is an unambiguous existing SSH alias:

```sh
woltspace dig grant newbox newbox --wolt "$WOLTSPACE_WOLT_NAME"
```

If the destination, wolt, or intended work is ambiguous, ask before granting or
connecting. Never guess a hostname or broaden an instruction about one machine
to another.

The grant records Woltspace consent; the SSH user's actual permissions remain
the authority. Do not create accounts, keys, tunnels, or broader server access
unless the human separately asks for that work.

## Connect safely

Start with a small read-only probe appropriate to the request, for example:

```sh
woltspace dig connect newbox -- hostname
woltspace dig connect newbox -- whoami
woltspace dig connect newbox -- command -v woltspace
```

Use `woltspace dig connect NAME -- COMMAND...` for bounded commands and
`woltspace dig connect NAME` only when an interactive shell is genuinely useful.
The command rechecks the approved SSH host, user, and port and requires strict
host-key verification.

Never disable `StrictHostKeyChecking`, accept a new host key on the human's
behalf, copy a private SSH key, or send credentials through chat, Wire, command
arguments, or handoff files. If SSH authentication or host verification fails,
report the exact non-secret failure and let the human repair their normal SSH
configuration.

## Bootstrap and hand off

Make changes only within the task the human authorized. Preserve existing data
and inspect before overwriting configuration.

The grant's `bootstrap_dir` is relative to the remote SSH user's home and
defaults to `.woltspace/bootstrap`. Create it when needed and leave concise,
human-readable artifacts for the future colony, such as:

- what was installed or configured;
- important paths and decisions;
- checks run and their results;
- incomplete work, open questions, and safe next steps.

Do not write into another wolt's private memory. Do not copy the visiting
wolt's identity, memories, sessions, credentials, or local configuration to the
remote machine. A colony seed may be installed there only when the human has
selected and authorized that seed; digging permission alone does not select one.

## Return and report

Disconnect when the requested work is complete or progress is blocked. Report
the destination, changes, verification, handoff path, and anything left undone.
Do not claim that `woltspace dig revoke NAME` removes real SSH access: it removes
only Woltspace's local grant. The owner must separately remove SSH keys,
accounts, or server authorization when underlying access should end.
