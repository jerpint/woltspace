# Woltspace 0.5.20

Python **0.5.20** is a release for 0.5. The separately distributed
**@woltspace/tui remains at 0.5.2**; no npm release is required.

## Thank you

- [@asharakeh](https://github.com/asharakeh) built the hold, the rule that
  idle sessions rest by use, and the ways to see and release holds
  (#559, #569, #570, merged as #571).
- [@ASalvail](https://github.com/ASalvail), with a first contribution: the
  fix for `woltspace session` in an ordinary terminal (#568).

## What's new

- **Idle sessions rest by use, and a wolt can hold its session.** A running
  session rests once nobody has used it for the idle window (Settings →
  Sessions, or `/idle` on Telegram: never, 1 hour, 4 hours, 1 day; the
  default is 1 day). Use means a message to or from the session, a spawn,
  `push-view`, or a keystroke or attach in tmux. Screen output does not
  count, so every engine behaves the same. A wolt with a long background
  task keeps its session up with
  `woltspace session hold <90m|6h|2d|forever> "<reason>"`; the new
  `woltspace-hold` skill tells it when to. You see every hold with its
  reason and can release it: `/holds` on Telegram, Sessions → Held in the
  lodge. A rested session wakes with its conversation on the next message
  (#554, #559, #569, #570, merged as #571).
- **`woltspace restart`.** One safe way for a wolt or a person to restart
  the lodge: it stops and starts on the same address, waits for health, and
  reports connectors that got worse or sessions that were not picked up
  again. If the start fails, the error names the exact command that brings
  the lodge back. `docs/updates.md` lists recommended permissions so a wolt
  can run `update`, `restart`, `status` and `doctor` without asking (#558).
- **Wolts set their own effort.** Ask your wolt for low, medium, high or
  xhigh effort; the new `woltspace-engine-settings` skill writes it in that
  wolt's own folder only, for Claude Code and Codex, so other wolts are not
  affected. The wolt's settings page says "Effort and other engine
  settings: ask your wolt." (#572).

## Fixes

- **A voice note from a wolt is never lost.** When the speech service
  stalls, `notify --speak` tries once more; if it still fails, the lodge
  sends the words as text with a note that the voice note failed, and the
  wolt is told not to send them again (#563).
- **`woltspace session` works from an ordinary terminal.** On a native
  install it failed outside a lodge session with
  `env: woltspace-python: No such file or directory`. It now runs on the
  same Python as the CLI (#568).
- **`woltspace update` works for a lodge on a non-default port next to
  another lodge.** A stopped lodge with no owner record no longer reads as a
  conflict with the lodge on 7777 (#558).

## Upgrade notes

```sh
woltspace update
```

No colony-data migration is required. Native upgrades stop and restart the
control plane while preserving tmux sessions. Container lodges update from the
host as before.

After upgrading, the first sweep (it waits for the lodge's health check)
rests every running session nobody has used for longer than the idle
window, 1 day unless you changed it. Sessions whose screens keep printing
are included. They wake with their conversation on the next message. A
wolt running a long job should take a hold first.

After upgrading, verify the lodge is healthy:

```sh
woltspace status
```
