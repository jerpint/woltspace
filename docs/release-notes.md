# Woltspace 0.5.12

Python **0.5.12** adds one command: `woltspace update`. Updating a native lodge
no longer means a uv command plus a stop and a start by hand. The separately
distributed **@woltspace/tui remains at 0.5.2**; no npm release is required.

## `woltspace update`

- **One command.** `woltspace update` finds the newest release on PyPI, asks to
  confirm, stops the lodge, installs the exact version with uv, starts the lodge
  again on the same address using the newly installed executable, then checks
  the version and the lodge's health. A lodge that was stopped stays stopped.
- **Check first.** `woltspace update --check` reports the installed and newest
  versions and changes nothing.
- **Choose the version.** `--version X` installs an exact release; `--pre`
  allows release candidates; `--yes` skips the prompt; `--json` for scripts.
- **Safe by default.** It refuses downgrades, yanked releases, container lodges
  (update those from the host) and installs that uv didn't make from PyPI
  (source checkouts, pip, custom uv sources).
- **Wolts can run it.** Tmux sessions survive the restart, so a wolt can update
  its own lodge and get the result back. The bundled update skill now uses this
  command for the mechanics, after reviewing release notes and migrations.
- **If the install fails**, the lodge is still started again, and both results
  are reported.

## Upgrade notes

0.5.11 does not have `woltspace update` yet, so this one upgrade is manual (stop
first; start with the same `--host/--port` if your lodge doesn't use the default):

```sh
woltspace stop
uv tool install --force 'woltspace==0.5.12'
woltspace start
```

From 0.5.12 on, `woltspace update` does it. No colony-data migration is
required. Native upgrades stop and restart the control plane while preserving
tmux sessions.

After upgrading, verify the lodge is healthy:

```sh
woltspace status
```
