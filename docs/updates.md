# Updating Woltspace

For native uv tool installations, run:

```sh
woltspace update
```

The command checks Woltspace on PyPI and the optional global `@woltspace/tui`
on npm independently. It prints all crossed release notes, migrations and
session impact, asks once, stages exact versions and dependencies, then applies
from the warmed caches. Only changed components are installed. The Python
installation preserves its existing extras, including `connectors`.

A small standalone updater runs under the base Python interpreter before uv
replaces its tool environment. It stops only a previously running control plane
when the Python package changes, restarts it, and checks versions, health,
previously running connectors and session adoption. An initially stopped lodge
stays stopped. A TUI-only update leaves the control plane running.

The tunnel and chat connectors are unavailable during the restart. tmux sessions
survive; existing sessions keep the instructions they loaded at startup. New
sessions load the updated platform instructions and skills.

Migration instructions are reviewed before applying and compared with the staged
wheel. They are not run automatically. The final report lists pending migration
actions separately. Failure reports list completed installs and recovery results;
there is no automatic rollback. The private result file path is printed.

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

A saved plan pins versions. Applying it does not fetch a new latest version.
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

Native uv tool installs using ordinary registry requirements are supported.
Custom uv sources, options, additional requirements, pip/source installs and
TUI installations outside npm global management need manual upgrades. The updater
does not install a missing TUI. It does not use sudo or change the user's shell.

In external/container isolation, native lifecycle commands including `update`
are absent. Update the container through host-side container tooling. Native
session-preservation claims do not apply to replacing containers.
