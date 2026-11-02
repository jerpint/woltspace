# Updating Woltspace

Woltspace has a thin update command for ordinary uv tool installations. It uses uv and the native lodge lifecycle; it is not a separate update engine.

## Ask a wolt

Ask “Check for Woltspace updates” or “Update Woltspace.” The bundled update skill reviews crossed release notes and migrations, obtains applicable consent, then uses this command for the mechanical lifecycle. A check alone does not authorize installation.

## Native update command

Check without changing anything:

```sh
woltspace update --check
```

After reviewing releases and migrations, update to the newest stable release or an exact reviewed release:

```sh
woltspace update
woltspace update --version VERSION
```

The command confirms interactively unless `--yes` is supplied. `--pre` permits the newest prerelease; `--json` returns machine-readable results. It refuses downgrades, yanked releases, containers, source/pip installs and custom uv sources.

For a running lodge it records status, stops, runs `uv tool install --force` with an exact version, restarts via the newly installed executable, then verifies version and health. An initially stopped lodge remains stopped. An install failure still triggers the restart attempt, and both failures are reported.

The browser, tunnel and messaging briefly disconnect, but native tmux sessions survive. Existing sessions retain their loaded instructions; new sessions load newly synced skills.

## Restart

```sh
woltspace restart
```

Stops a running lodge and starts it again on the same host and port, then waits up to thirty seconds for it to be healthy and reports connectors that got worse or sessions that were not adopted. It refuses a stopped lodge (use `woltspace start`) and container lodges. If start fails, the error names the exact `woltspace start` command that brings the lodge back. tmux sessions survive, as with an update.

Prefer it over `woltspace stop && woltspace start` from inside a wolt: a wolt that runs `stop` alone leaves the lodge down, and cannot notify anyone until someone starts it again.

## Recommended permissions

Wolts that keep the lodge up to date need to run these commands without a prompt. For Claude, add them to the owner's `~/.claude/settings.json`, which covers every wolt on the machine:

```json
{
  "permissions": {
    "allow": [
      "Bash(woltspace update:*)",
      "Bash(woltspace restart:*)",
      "Bash(woltspace status:*)",
      "Bash(woltspace doctor:*)"
    ]
  }
}
```

Claude's auto mode honours these rules too. `woltspace start` and `woltspace stop` are left out on purpose: they are for the person at the keyboard.

What you accept with them: `update:*` also covers `--pre` and `--version X`, so a wolt can install a prerelease without asking (downgrades and yanked releases are still refused). `restart:*` lets any wolt, including one acting on text it was sent, bounce the lodge; tmux sessions survive, so the cost is a short outage.

On a machine where two lodges share one install, updating one replaces the package under the other. `update` reports this and exits 1; restart the other lodge so it loads the new version.

## Manual native upgrade

Older releases without the command can be upgraded once with the underlying steps. Review the [published release notes](https://github.com/jerpint/woltspace/releases), PyPI withdrawal state and crossed migrations, then run:

```sh
woltspace status --json
woltspace stop
uv tool install --force 'woltspace==VERSION'
woltspace start
woltspace --version
woltspace status --json
```

Use the installed native CLI and same lodge environment. If uv fails after the stop, still attempt start and report both outcomes. If the lodge was already stopped and should remain stopped, skip stop/start. Woltspace 0.5.4 and newer has no extras to preserve. Never overwrite a custom source or options with this example; follow that installation's own recipe.

Normal uv resolution and cache behavior applies. A failed replacement can leave the CLI unable to start; repair it with uv, then start manually. There is no automatic rollback or migration execution. A package downgrade cannot reverse a migration and is not part of this workflow.

## TUI and container updates

The npm `@woltspace/tui` has independent versions. Update it separately after reviewing its release notes and minimum lodge version.

External/container deployments are updated through host container tooling, not with native lifecycle commands inside the container. Source and pip installs use the workflow for their installation method.

Validate genuine transitions in a disposable lodge before updating a live lodge.

Since 0.5.4, `uv tool install woltspace` includes all Telegram and Slack connector
dependencies. Channels still run only when enabled in your configuration.
