---
name: update
description: Check or update native Woltspace; review release context and invoke the Python-only update CLI on the user's behalf. Container lifecycle belongs to host tooling.
user_invocable: true
---

# Update Woltspace

The skill owns release context and consent. The CLI performs the mechanical
Python update, stages before downtime, restarts and verifies the lodge.
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

Run `woltspace update --to VERSION` using the exact reviewed version. This is an
explicit install instruction and does not prompt; use it only with authorization.
Do not issue separate installer or restart commands. Bare `woltspace update` is
the human convenience path: check the latest version, show impact and confirm.

Report the observed installed version, health, connector/session verification
and any recovery outcome. A nonzero exit is not success. Do not blindly retry
or claim rollback. Migration instructions are not executed by the CLI: carry
out only actions covered by user permission and workspace rules, otherwise
report them as pending. Package verification does not mean migrations are done.

In external/container isolation, native lifecycle commands are absent; hand
updates to host-side container tooling. Native start syncs the platform skill;
there is no separate skill installation step.
