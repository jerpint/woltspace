---
name: digging
description: Use owner-approved SSH access to inspect or set up a remote machine, bootstrap a Woltspace colony, and leave a durable local handoff. Use when a human asks a wolt to SSH into, dig into, configure, or prepare another machine. Do not use for Wire pairing or cross-colony IWCL.
---

# Dig into an owned machine

Digging means temporarily using the owner's existing SSH access. The wolt stays
resident in its home colony: connect, do the authorized work, leave a local
handoff for future wolts on that machine, disconnect, and report home.

Prefer `woltspace dig` over invoking `ssh` directly whenever the digging command
is available. The wrapper makes the intended wolt and destination visible,
rechecks the resolved target, preserves the handoff convention, and records a
small non-secret audit trail. It does not create a security boundary against a
wolt running as the same Unix user.

## Find or create the approval

Run `woltspace dig list --json`. Use an existing grant only when its wolt and
destination match the human's request.

If none matches and the named destination is an unambiguous existing SSH alias,
set up the dig grant yourself rather than asking the human to remember CLI
syntax. Choose a short lowercase grant name and run:

```sh
woltspace dig grant newbox newbox --wolt "$WOLTSPACE_WOLT_NAME"
```

Creating the grant only records a pointer to existing SSH access; it copies no
key, certificate, agent credential, or token. If the destination, wolt, or
intended work is ambiguous, ask before granting. Never guess a hostname or
browse unrelated SSH destinations looking for somewhere to connect.

The grant records Woltspace consent; the SSH user's actual permissions remain
the authority. Do not create accounts, keys, tunnels, or broader server access
unless the human separately asks for that work.

## Connect safely

Immediately before the first connection in a task, show the human the grant
name and resolved `user@host:port`, briefly state the intended work, and ask for
confirmation. A stored grant is not standing permission to connect.

Skip that confirmation only when the human's current instruction explicitly
says that permission is not required, says to proceed without asking, or gives
equally clear authorization to connect immediately. Do not infer a permanent
waiver from an earlier task or from the mere existence of SSH access. Once the
human confirms a task, do not repeatedly ask for every bounded command needed
to complete that same task unless the target or scope changes.

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
