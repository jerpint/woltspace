# Updating Woltspace

Woltspace uses its package manager for upgrades. There is no self-update CLI,
custom installer, or separate updater to bootstrap.

## Ask a wolt

From a native lodge session, ask: “Check for Woltspace updates” or “Update
Woltspace.” The bundled update skill reviews the chosen release and crossed
release notes/migrations, obtains applicable consent, and runs standard uv and
lodge lifecycle commands. A check alone does not authorize installation.

The native tmux session survives the control-plane stop, so the wolt can upgrade
and restart from within the lodge. The browser, tunnel and messaging connectors
briefly disconnect; the wolt reports once routing is restored. Existing sessions
keep loaded instructions. Native start syncs platform skills; new sessions load
the updated instructions. The skill checks the installed version and recovery
and reports gaps rather than assuming success.

## Manual native upgrade

For an ordinary uv tool install, review the [published release notes](https://github.com/jerpint/woltspace/releases)
and migrations for the target and every crossed version, then run:

```sh
woltspace stop
uv tool upgrade woltspace
woltspace start
woltspace status
```

Use the installed native CLI and keep the same lodge environment/settings for
stop and start. These are separate steps: if uv fails after stopping, still
attempt start and report/resolve the install failure. If already stopped and
you intend it to remain stopped, skip stop/start. There is no automatic recovery
wrapper around these commands.

uv upgrades respect installed version constraints and retain installation
settings. If pinned to an older version, or choosing a specific reviewed release,
replace the upgrade step with:

```sh
uv tool install --force 'woltspace==VERSION'
```

Replace VERSION with the reviewed published version, preserving your installed
extras. The wolt uses this exact-version path to avoid installing a different
latest version than the one authorized. Do not overwrite custom sources/options
with this example; preserve your recipe or use its documented upgrade procedure.
Do not select withdrawn/yanked releases or downgrade as a routine upgrade.

Normal uv resolution/cache behavior applies; downloads happen while the lodge
is down. A failed replacement can leave the CLI unable to start. Repair the
package with uv, then run `woltspace start` manually. No automatic rollback or
migration execution is provided. Manual downgrade requires checking whether the
older code can read current state; a compatible backup may be needed. Installing
older code alone does not reverse migrations.

Run `woltspace --version` and `woltspace status --json` afterwards and check your
messaging channel. Connector diagnostics can retain stale state/errors; compare
with the pre-update baseline and allow startup time before concluding recovery
failed. A successful package installation is not proof every connector resumed
or that required migration actions are complete.

## Getting the new skill

Older users already have uv and lodge stop/start commands; they can use the
manual flow above to install the release containing this revised skill. No new
update command is required. Start syncs the bundled skill; open a new session to
load it. The website installer remains for fresh installations.

## TUI and container updates

The npm `@woltspace/tui` is a separate distribution with independent versions.
Update it separately after reviewing its release notes and minimum lodge version.
A newer TUI can refuse to start against an older lodge; upgrade the lodge to the
required minimum first. The TUI checks its minimum lodge version, while the lodge
does not enforce a TUI version. A dedicated TUI updater is deferred.

For external/container deployments, update through the host's container tooling;
do not apply native uv lifecycle commands inside the container. For source/pip
installations, use the workflow for that installation method.

Validate a genuine version transition in a disposable lodge before updating the
live lodge. Earlier container checks were no-op checks of the now-removed updater
and do not establish that this skill workflow has been tested end to end.
