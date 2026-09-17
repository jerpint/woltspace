# Updating Woltspace

For native uv tool installations:

```sh
woltspace update
```

The command checks PyPI, prints the installed/target Python version and restart
impact, asks for confirmation, then updates. If already current, nothing changes.
There are two options:

```sh
woltspace update --check           # inspect without applying
woltspace update --to 0.5.4        # install this exact published version
woltspace update --check --to 0.5.4  # inspect that exact version without applying
```

`--to VERSION` is an explicit installation instruction and does not prompt.
It validates a stable three-part version (optionally `.postN`), confirms that
version exists on PyPI and never resolves latest instead. Downgrades are refused.
Bare update requires interactive confirmation when a change is available; in
noninteractive use, choose a target explicitly. The separate npm TUI is not
inspected or modified.

## Context and consent

The CLI handles the mechanical install; it does not fetch release notes, judge
whether they have been reviewed, or execute migrations. Before updating, a human running the bare command must read
the target and every crossed release's [published GitHub release notes](https://github.com/jerpint/woltspace/releases)
and migration instructions themselves; the CLI does not display migration prose. A wolt
uses the bundled update skill to handle that context and authorization, then
invokes `woltspace update --to VERSION` for the exact reviewed target. Routine
checks require no install consent. Installation requires session authorization
or applicable standing permission; a check request alone does not grant it.
Migration instructions live under `container/migrations/` at each release commit.
Required manual actions remain separate from package installation.

## Installation and restart

A standalone updater runs under base Python before uv replaces its environment.
It stages the target package/dependencies in an isolated environment while the
lodge is still running, freezes dependency constraints, and preflights offline
resolution. It serializes updates and rechecks the control-plane identity before
stopping. It preserves installed extras and installs from warmed caches.

Only a previously running control plane is stopped and restarted. An initially
stopped lodge stays stopped. The tunnel and connectors briefly go offline;
tmux sessions survive. Existing sessions retain loaded instructions; new sessions
load the new skills and prompts after native start syncs platform skills.

Verification checks the observed Python version, health, previously running
connectors and session adoption. If an install fails after stopping, the updater
attempts to start the lodge again. Reports include completed actions, observed
version and recovery results; there is no automatic rollback. A verified package
update does not imply migrations are complete.

Private reports remain under `.space/platform/updates/`, with their paths printed.
Owned staging environments/caches are removed on completion or failure; a hard
process kill can leave temporary files. Internal worker handoff files are not a
public saved-plan interface.

## TUI updates are separate

The user owns the separate npm `@woltspace/tui` upgrade. Review its release notes
and minimum lodge version first: a newer TUI can refuse to start against an older
lodge. Update the lodge to that minimum before using the newer TUI. The TUI checks
its minimum lodge version; the lodge does not enforce a TUI version. A dedicated
TUI updater is deferred.

## One-time bootstrap for older users

Older releases lack this command. Once a release containing it is published,
review its notes and migrations, run the old CLI's `woltspace stop`, install the
exact reviewed version with `uv tool install --force 'woltspace[connectors]==VERSION'`,
then run `woltspace start` and `woltspace status`. Preserve other installed extras.
This branch is not a published release. Do not leave the old control plane
running over replaced files. Subsequent upgrades use `woltspace update`.

## Supported installations

Only native uv tool installs with ordinary registry requirements are supported.
Custom sources/options, additional requirements and pip/source installs need
manual upgrades. The command does not use sudo or change shell configuration.
In external/container isolation native lifecycle commands, including update,
are absent; container lifecycle belongs to host-side container tooling.

Before using the updater on a live lodge, test a genuine version transition in a
disposable lodge. The earlier container smoke test was a no-op and did not verify
staging, installation, restart or recovery end to end.
