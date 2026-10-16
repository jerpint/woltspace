---
name: hold
description: Keep this session from resting while a background task you started must keep running (a build, a dev server, a watcher, a long test run). Only for that, never just to stay available.
---

# Hold: keep a session up for a background task

The lodge rests a session once nobody has used it for the idle window the
owner set (1 hour, 4 hours or 1 day). A rest stops the session's tmux and
everything running inside it. The conversation is kept, and the next message
wakes the session with its full history.

A rest normally costs you nothing. The exception is a task still running in
the background: it dies with the session. A hold tells the lodge not to rest
this session yet, because something is running in it.

## When to hold

Hold **only** when a task must keep running while nobody talks to this
session:

- a background job you started: a build, a dev server, a file watcher, a
  training run, a long test suite;
- one command you expect to run longer than the idle window.

Do **not** hold:

- to stay "available": a resting session wakes on the next message, with its
  conversation;
- while you are talking anyway: messages to you, typing in your terminal, and
  your own `notify`, `woltspace session send`, `woltspace session spawn` and
  `push-view` all count as use;
- "just in case".

## Usage

```bash
woltspace session hold 6h "npm run dev on :3000"      # hold for 6 hours
woltspace session hold forever "tunnel for the demo"  # until you release it
woltspace session hold off                            # release the hold
woltspace session hold                                # show this session's hold
```

- **Durations:** `90m`, `6h`, `2d`, or `forever`.
- **The reason is required.** Name the task that is running. The owner sees
  every held session with its reason (in the lodge: Sessions, then Held; on
  Telegram: `/holds`) and can release it. The lodge log names it too.

## Rules

- **Release the hold (`hold off`) as soon as the task ends.** A forgotten hold
  keeps a session, with its memory and processes, up for nothing. A `forever`
  hold never ends on its own.
- **Prefer a duration to `forever`**, and pick the shortest one that covers
  the task.
- **A hold covers this session only.** Run it from inside the session that
  owns the task: it reads `$WOLTSPACE_WOLT_SESSION`. Use `--session <id>`
  otherwise.
- **A hold also ends when the session stops for any other reason** (it died,
  or someone stopped it), because the task it covered died with the session.
  If you restart the task, take a new hold.
- **Holding a session that is not running fails.**
