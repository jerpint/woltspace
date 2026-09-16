# Updating Woltspace

For native uv tool installations, run:

```sh
woltspace update
```

The command updates only the Woltspace Python package from PyPI. It prints
all crossed Python release notes, migrations and session impact, asks once,
stages the exact version and dependencies, then applies from warmed caches.
Missing Python release notes block the update. Existing extras, including
`connectors`, are preserved. The separately distributed npm TUI is not checked
or updated; TUI updates remain a separate manual operation for now. The user
owns that separate npm upgrade. Review its release notes and minimum lodge
version first: a newer TUI can refuse to start against an older lodge. Update
the lodge to the required minimum before using that TUI. The existing TUI
checks its minimum lodge version; the lodge does not enforce a TUI version.

A small standalone updater runs under the base Python interpreter before uv
replaces its tool environment. It stops only a previously running control plane
when the Python package changes, restarts it, and checks versions, health,
previously running connectors and session adoption. An initially stopped lodge
stays stopped.

The tunnel and chat connectors are unavailable during the restart. tmux sessions
survive; existing sessions keep the instructions they loaded at startup. New
sessions load the updated platform instructions and skills.

Migration instructions are reviewed before applying and compared with the staged
wheel. They are not run automatically. The final report lists pending migration
actions separately. Failure reports list completed installs and recovery results;
there is no automatic rollback. The private result file path under `.space/platform/updates/` is printed.
Owned temporary environments and caches are removed on completion or failure;
reports remain. A hard process kill can still leave temporary staging files.

## Ask a wolt

Ask a wolt to check or update Woltspace. The platform update skill invokes this
same command on your behalf. It can check routinely without installation consent.
Application requires approval or explicit standing permission covering the plan.

For agents and automation:

```sh
woltspace update --check --json --plan ./update-plan.json
# After reviewing and authorizing that plan:
woltspace update --apply-plan ./update-plan.json --yes
```

A saved plan pins the Python version. Plans from the earlier combined Python/TUI
format are rejected; create a new plan. Applying it does not fetch a new latest version.
Changed installation or control-plane identity invalidates the plan. Keep plan
and result files private; they include local runtime metadata.

## One-time bootstrap for older users

Older releases do not contain this command. After a release containing it is
published, users must upgrade manually once. Review that release's notes and
migrations first, then run the old CLI's `woltspace stop`, install the exact
reviewed release with `uv tool install --force 'woltspace[connectors]==VERSION'`,
and run `woltspace start` followed by `woltspace status`. Replace `VERSION` with
the published release that introduces this feature; this branch is not a release.
Preserve any other extras you installed. Do not mutate a live tool environment
while leaving the old control plane running over replaced files.

Subsequent upgrades use `woltspace update`. The website setup script remains for
fresh installation.

## Supported installations

Native uv tool installs using ordinary registry requirements and stable
three-part release versions (optionally `.postN`) are supported.
Custom uv sources, options, additional requirements and pip/source installs
need manual upgrades. The updater does not inspect or modify TUI installations. It does not use sudo or change the user's shell.

In external/container isolation, native lifecycle commands including `update`
are absent. Update the container through host-side container tooling. Native
session-preservation claims do not apply to replacing containers.
