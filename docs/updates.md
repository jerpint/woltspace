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
version exists on PyPI and never resolves latest instead. Downgrades and releases marked yanked on PyPI are refused.
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

The command wraps the ordinary package upgrade sequence:

1. Stop a running lodge's control plane.
2. Run `uv tool install --force --python BASE_PYTHON 'woltspace[EXTRAS]==VERSION'`,
   preserving the installed extras and using uv's normal dependency resolution
   and cache.
3. Attempt to start the control plane again, even if installation failed.

A previously stopped lodge stays stopped. A small helper runs under base Python
outside the tool environment being replaced; it is temporary process plumbing,
not a staging environment or saved-plan interface. Concurrent updates are refused.
The helper is cleaned up when it exits.

The tunnel and connectors are offline during installation, including downloads.
There is no pre-download rehearsal, frozen dependency set or offline install
mode. A network/dependency failure can happen while the lodge is stopped. If a
failed install leaves the CLI unusable, the automatic restart can fail too;
repair the package with uv and then run `woltspace start` manually.

The command checks uv's exit result, the restart command's result and the observed
Python version. It does not verify connector recovery or session adoption.
A successful exit is not proof that every bot connector resumed: check
`woltspace status` and your messaging channel afterwards. tmux sessions survive
a control-plane stop; existing sessions retain loaded instructions. New sessions
load updated skills after native start syncs them.

There are no durable updater reports or automatic rollback. Migration actions
are separate. A rollback requires a manual pinned
`uv tool install --force 'woltspace[connectors]==OLDER_VERSION'`, preserving your
installed extras and stopping/restarting the control plane around replacement.
First verify the older code can read the current on-disk state; migrations may
require restoring a compatible backup. An older package alone does not reverse
migrations.

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
