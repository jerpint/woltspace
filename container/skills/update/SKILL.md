---
name: update
description: Check or upgrade native Woltspace from a running lodge session using uv; review releases, handle consent, stop/start and verify recovery. Container lifecycle belongs to host tooling.
user_invocable: true
---

# Update Woltspace

Use standard uv commands, not a self-update engine. This workflow updates the
Python package only; the separate npm `@woltspace/tui` is outside its scope.
See [manual update instructions](../../../docs/updates.md).

## Review and authorize

Check the installed version with `woltspace --version` and choose an exact
published Python release. Review its PyPI availability/withdrawal status, all
crossed GitHub release notes (`vVERSION`) and migrations under
`container/migrations/` at the reviewed release commit. Do not infer migration
absence from a patch version. Confirm the target is not yanked/withdrawn before
installing: an exact pin can select a release that was pulled for being broken.
Never install a version lower than the installed one in this update workflow;
a package downgrade cannot reverse a migration. If context cannot be verified, report the gap
before installing. If already current, say so and finish.

A check request is not installation consent. Summarize the chosen version,
breaking behavior, migration actions and downtime. Obtain approval unless
session or applicable standing permission already covers them; do not ask again
when it suffices. New incompatible requirements need human input. Do not create
a schedule unless requested.

## Upgrade from the session

Ensure commands address the installed native CLI and same lodge environment,
not a checkout launcher or a container runtime. Confirm uv manages this install
and preserve its extras/installation recipe. Source/pip installations need their
own upgrade workflow; do not convert them with uv tool install.
Custom sources/options must not be silently replaced with a registry install; if the recipe is unclear, stop and
report it. There is no need to install a new update command first.

Run `woltspace status --json` before applying. Record whether it is running and
retain connector diagnostics (`state`, `restarts`, `last_exit_code`, `started_at`,
`error`) and session adoption. Report pre-existing degraded conditions. If lodge
state cannot be established or it is unhealthy/conflicting, resolve that before
stopping or replacing the package.

Warn that the control plane, tunnel and connectors go offline during installation,
including downloads. The native tmux session survives and can execute uv and
start the lodge again; browser/chat routing returns after restart. Existing
sessions retain loaded instructions; new sessions load newly synced skills.

If running, run `woltspace stop`; confirm it succeeded before installing.
Install the exact reviewed version using the ordinary uv command, for example:

```sh
uv tool install --force 'woltspace[connectors]==VERSION'
```

Replace VERSION with the reviewed version and preserve all installed extras.
Do not resolve latest again after authorization. `uv tool upgrade woltspace` is
the ordinary latest-upgrade command, but respects installed version constraints;
use the exact install command for this reviewed-version workflow.

If you stopped the lodge, attempt `woltspace start` even when installation fails.
An initially stopped lodge stays stopped. If replacement damaged the CLI and
start fails, report both failures and the need for manual uv repair. Do not
blindly retry or claim rollback. A nonzero installer exit is not success.

## Check and report

Run `woltspace --version` and `woltspace status --json` afterwards. Confirm the
observed version matches the chosen release and compare health, connector
diagnostics and adoption with the baseline. New `started_at` and reset counters
are expected after restart; state/errors can be stale even while messaging
works. Distinguish prior conditions, transient startup and new errors.

For incomplete startup, re-check about every 5 seconds for up to 30 seconds.
Report persistent conditions with diagnostics and uncertainty about messaging;
do not treat a sticky flag alone as proof of an outage or silently ignore it.
If status is unavailable, explicitly report incomplete verification. Do not
report a stopped lodge as failed recovery when it was stopped initially.

Migration instructions are not executed by uv. Carry out only actions covered
by user permission and workspace rules; report others as pending. Package
installation does not mean migrations are done. Native start syncs skills;
there is no separate skill installation step.

For external/container lodges, hand updates to host-side container tooling.
Do not run native stop/install/start against a container lodge or live host
from inside the container.
