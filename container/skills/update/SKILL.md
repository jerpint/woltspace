---
name: update
description: Check or upgrade native Woltspace from a running lodge session using uv; review releases, handle consent, stop/start and verify recovery. Container lifecycle belongs to host tooling.
user_invocable: true
---

# Update Woltspace

Use `woltspace update` as the thin, uv-backed mechanical wrapper. This workflow updates the Python package only; the separate npm `@woltspace/tui` is outside its scope. See [manual update instructions](../../../docs/updates.md).

## Review and authorize

Start with `woltspace update --check`. Review the target's PyPI availability/withdrawal state, all crossed GitHub release notes (`vVERSION`) and migrations under `container/migrations/` at the reviewed release commit. Never infer migration absence from a patch version or downgrade: installing older code cannot reverse migrations. If context cannot be verified, report the gap.

A check is not installation consent. Summarize the chosen version, breaking behavior, migration actions and downtime. Obtain approval unless applicable standing permission already covers it. New incompatible requirements need human input.

## Upgrade from the session

Ensure the command addresses the installed native CLI and same lodge environment, not a checkout or container runtime. It refuses source/pip/custom uv recipes rather than replacing them with a registry install. Containers remain a host-tooling responsibility.

Record current `woltspace status --json`, especially running state, connector diagnostics and session adoption. Report pre-existing degradation. A running update briefly takes the control plane, tunnel and connectors offline; tmux sessions survive. Existing sessions retain loaded instructions, while new sessions receive newly synced skills.

After review and consent, run the exact chosen version:

```sh
woltspace update --version VERSION --yes
```

The command revalidates yanked and downgrade boundaries, stops only a running native lodge, installs the exact version through uv, and restarts using the newly installed executable. It attempts restart even when installation fails. An initially stopped lodge remains stopped. Do not blindly retry or claim rollback.

`notify` uses the lodge API and is unavailable during the outage. Hold the report until restart rather than treating that expected failure as a new fault.

## Check and report

Read the command's final old/new version, health and degradation report. It rechecks startup every five seconds for up to thirty seconds. Distinguish prior conditions, transient startup and new errors; a sticky connector flag alone is not proof of an outage.

Migration instructions are not executed by uv. Carry out only actions covered by user permission and workspace rules, and report the rest as pending. Package installation does not mean migrations are done.

If the old release predates `woltspace update`, follow the manual uv procedure in the linked documentation once. External/container lodges stay with host tooling.
