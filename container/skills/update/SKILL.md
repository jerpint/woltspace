---
name: update
description: Check or update native Woltspace; review release context and invoke the Python-only update CLI on the user's behalf. Container lifecycle belongs to host tooling.
user_invocable: true
---

# Update Woltspace

The skill owns release context and consent. The CLI performs the mechanical
Python update: stop a running control plane, install through uv, attempt restart
even on install failure, and check the installed version. Downloads happen during
downtime; a failed install can also leave the CLI unable to restart.
The separate npm `@woltspace/tui` is outside this workflow.

Run `woltspace update --check` to inspect the installed and available Python
versions without applying. Routine checks need no install consent. If the
command is unavailable, explain the [one-time bootstrap](../../../docs/updates.md).
Do not substitute a hand-written installer/restart sequence.

Before applying, choose one exact stable three-part target. Review its published
GitHub release notes and every crossed Python release (`vVERSION` tags), including
breaking behavior and migrations under `container/migrations/` at the reviewed release commit. Do not infer migration absence from patch versions. If notes or
migration context cannot be verified, explain what is missing and stop before
installation. Remember the chosen version; do not switch to latest at apply time.
Use `woltspace update --check --to VERSION` to inspect that exact target without
applying, when confirming its availability.

A check request is not installation consent. Obtain authorization covering that
version, downtime and required manual actions unless existing session or standing
permission already covers them. Do not ask again when authorization suffices.
New incompatible requirements need human input. Do not create a schedule unless
requested.

Tell the user the control plane, tunnel and connectors briefly go offline.
tmux sessions survive, but existing sessions keep loaded instructions; new
sessions load updated skills and prompts.

Before applying, run `woltspace status --json` and retain the connector diagnostics
(`state`, `restarts`, `last_exit_code`, `started_at`, `error`) and session adoption.
Report any pre-existing non-running/degraded connector as a known prior condition.
These fields are reported diagnostics: `state` and recorded errors can be stale,
even when messaging works. Do not treat state alone as proof of an outage or
silently ignore it. If the baseline is unavailable, report that verification gap.

Run `woltspace update --to VERSION` using the exact reviewed version. This is an
explicit install instruction and does not prompt; use it only with authorization.
Do not issue separate installer or restart commands. Bare `woltspace update` is
the human convenience path: check the latest version, show impact and confirm.

Report the observed installed version and any restart failure. After updating,
run `woltspace status --json` and compare health, all connector diagnostics and
session adoption with the baseline; the updater does not verify those. A new
`started_at` is expected after restart; reset counters or a changed state alone
are not failures. Distinguish prior conditions, transient startup and new errors.
If a connector is not yet running or adoption is incomplete, re-check at roughly
5-second intervals for up to 30 seconds before concluding recovery is incomplete.
Report conditions that persist, including their diagnostics and any uncertainty
about actual messaging availability; do not call a sticky flag proof of failure.
If status cannot be obtained, explicitly report incomplete verification. A nonzero exit is not success. Do not blindly retry
or claim rollback. Migration instructions are not executed by the CLI: carry
out only actions covered by user permission and workspace rules, otherwise
report them as pending. Package verification does not mean migrations are done.

In external/container isolation, native lifecycle commands are absent; hand
updates to host-side container tooling. Native start syncs the platform skill;
there is no separate skill installation step.
