---
name: engine-settings
description: Change your own engine settings, such as effort (low, medium, high, xhigh), when the user asks. Writes the setting in your own folder only, never in a file other wolts share.
---

# Engine settings: change your own effort

When the user asks to change your effort, use the settings or model pin for
your engine below. Write only inside your wolt folder.

## Keep settings per wolt

Native sessions share the owner's engine homes, including `~/.claude/`,
`~/.codex/`, and Antigravity's `~/.gemini/antigravity-cli/settings.json`.
Use your own wolt folder for changes. Find it through `$WOLTSPACE_WOLT_HOME`
when the session runs in another directory.

## Claude

Persistent, from your next session:

- File: `.claude/settings.json` in your wolt folder.
- Key: `"effortLevel"`, one of `low`, `medium`, `high`, `xhigh`.
- `max` can't be saved: Claude ignores `"effortLevel": "max"` in a settings
  file and starts at its default. `max` only works for one session (below).
  If the user asks for max, save `xhigh` and tell them how to get max now.
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
- It must be a **top-level key, above the first `[table]` line**. The lodge
  keeps tables such as `[shell_environment_policy]` and `[projects."..."]`
  in this file. A key appended at the end lands inside the last table, and
  Codex won't read it as the effort setting. Put it on the first line (or
  replace the existing top-level line), and keep every other line.
- Check it parses, with the key at the top level:
  `python3 -c 'import tomllib;print(tomllib.load(open(".codex/config.toml","rb")).get("model_reasoning_effort"))'`
  must print your level.

Codex reads a folder's config only once the folder is trusted. Wolt folders
the lodge starts are trusted already. The status line shows the result:
`GPT-... high`.

## Antigravity

1. Run `agy models` to find the ID for the requested effort variant of your
   model. If the model has no matching variant, report the available choices
   and keep the current pin.
2. With Antigravity as your wolt's effective engine, run
   `woltspace wolt set "$WOLTSPACE_WOLT_NAME" --model "<full-ID>"`.
   Check that the result reports Antigravity and the chosen ID for the next
   session.

Interactive `/effort` saves a default in the owner's shared Antigravity
settings. The standalone `agy --effort` flag applies to one invocation.
Use the model pin for Woltspace sessions. [Google's effort reference](https://antigravity.google/docs/cli/commands/effort/)

## Other engines

Look for the engine's own per-project config file. If it has none, report
that the skill has no per-wolt setting for this engine.

## Tell the user

Say what you changed, in which file, and that it applies to new sessions.
For an immediate change in Claude, the user can use `/effort` and press **s**.
