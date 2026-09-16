---
name: update
description: Check or update native Woltspace on the user's behalf through woltspace update, including routine checks and authorized application. Container lifecycle belongs to host tooling.
user_invocable: true
---

# Update Woltspace

Use the installed `woltspace update` CLI. It owns version resolution, staging,
installation, control-plane restart and verification. The `woltspace` wheel and
optional globally installed `@woltspace/tui` have independent versions.

## Check and review

Run `woltspace update --check --json --plan <private-plan-path>` to inspect and
save a plan without applying. Routine checks need no update consent. Use a path
inside the wolt's private workspace. If the CLI is unavailable, explain the
[one-time manual bootstrap](../../../docs/updates.md); do not
silently implement another update procedure.

Report every component marked `skipped` and its reason; do not call a skipped
TUI current or updated. Missing TUI notes retain that installed version while
a reviewed wheel update may proceed. Missing wheel notes still refuse.
If `changed` is false, report installed versions and any skipped update, then finish. Otherwise summarize
both components, every crossed release note and every migration in the plan.
Do not infer safety or migration absence from a patch version. Explain any
breaking behavior and outstanding manual actions. The migration prose comes
from `container/migrations/` at the reviewed release commit and is checked
against the staged wheel before stopping anything.

## Apply the reviewed plan

A request to check is not a request to apply. Obtain approval for the reviewed
plan unless this session already has explicit authorization covering the update
or the user has given applicable standing update permission. Do not ask again
when existing authorization is sufficient. Standing permission must cover the
plan's disruption and migration requirements; a new incompatible requirement
needs human input. Do not create a schedule unless requested.

Tell the user that the control plane, tunnel and connectors will briefly go
quiet. tmux sessions survive. Sessions already running retain their loaded
instructions; start a new session to use changed instruction bodies.

Run `woltspace update --apply-plan <private-plan-path> --yes`. This is the same
engine as the human's bare `woltspace update`, with approval already handled.
Do not issue separate installer or restart commands. Apply never resolves
`latest` again. If installation or lodge identity changed since review, make a
new plan and review the difference before retrying.

## Report

Report the exact installed versions, health, connectors and session adoption
from the updater's result. A nonzero exit or partial failure is not success;
name what completed, what failed and the recorded recovery result. Do not
blindly retry an installation or claim rollback happened.

Migration documents are instructions, not automatically executed scripts.
After package verification, carry out only actions covered by the user's
permission and allowed by the wolt's workspace rules. Otherwise clearly report
them as pending; a verified package update does not mean migrations are done.

In external/container isolation, hand lifecycle work to host-side container
tooling. Do not use a legacy rebuild/latest command or attempt a native update
inside the container. The platform skill sync happens during native start;
there is no separate skill installation step.
