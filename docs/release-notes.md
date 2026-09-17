Python **0.5.5** and **@woltspace/tui 0.5.2** ship shared lodge skills and a fix
for terminal TUI sessions ignoring existing Auto grants.

- Add local `.space/shared-skills/` with mandatory `lodge-` names, per-wolt
  overrides, lifecycle refresh and a bundled setup guide.
- Let the server resolve execution policy when the terminal TUI caller has not
  explicitly chosen one, so approved Auto grants apply.
- Pin the Python/TUI version pair in Docker builds.
- Add focused Python 3.11/3.13 and Node CI, package-content verification, and
  owner-approved OIDC publishing for both registries.

The TUI is distributed separately: upgrading Python alone does not install the
terminal fix. Review shared-skill ownership in `docs/shared-skills.md` and release
approval/recovery in `docs/releasing.md`. The full historical test suite remains
outside the release gate while its environment assumptions are refactored.
