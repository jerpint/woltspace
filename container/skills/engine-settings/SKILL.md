---
name: engine-settings
description: Change your own engine settings, such as effort (low, medium, high, xhigh, max), when the user asks. Writes the setting in your own folder only, never in a file other wolts share.
---

# Engine settings: change your own effort

Your engine (Claude, Codex, ...) has settings the lodge does not manage, such
as how much effort you put into each answer. When the user asks you to change
one, set it yourself, in your engine's own config file **inside your wolt
folder**.

## Never touch the shared files

On a native lodge every wolt runs under the owner's home, so `~/.claude/` and
`~/.codex/` are shared by all wolts. A change there changes every wolt.
Only write inside your wolt folder (your current working directory).

## Claude

Persistent, from your next session:

- File: `.claude/settings.json` in your wolt folder.
- Key: `"effortLevel"`, one of `low`, `medium`, `high`, `xhigh`, `max`.
- Merge it into the existing JSON. Keep every other key; the file may hold
  permissions and other settings.

This folder setting wins over the owner's own default, including a per-model
default. The start screen shows it: `Opus ... with low effort`.

Right now, for this session only: the user can type `/effort`, pick a level
and press **s** ("this session only"). Pressing Enter instead saves the level
to the owner's shared settings and changes every wolt: tell the user to
press **s**.

## Codex

Persistent, from your next session:

- File: `.codex/config.toml` in your wolt folder.
- Key: `model_reasoning_effort = "<level>"`, using a level your model lists
  (`low`, `medium`, `high`, ...).
- Keep every other line of the file.

Codex reads a folder's config only once the folder is trusted. Wolt folders
the lodge starts are trusted already. The status line shows the result:
`GPT-... high`.

## Other engines

Look for the engine's own per-project config file. If it has none, say so
and don't edit anything outside your folder.

## Tell the user

Say what you set, in which file, and that it applies from your next session.
To use it now, they can stop this session and start a new one (or, on Claude,
use `/effort` and press **s**).
