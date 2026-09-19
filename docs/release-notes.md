Python **0.5.6** ships harness-first onboarding and Git-friendly Colony Seeds.
The separately distributed **@woltspace/tui remains at 0.5.2**.

- Choose any registered harness when opening a new lodge or creating a wolt,
  with lodge defaults and per-wolt overrides available in Settings. Harness
  authentication remains the responsibility of its normal CLI flow.
- Create, inspect, and install portable starter colonies with `woltspace seed`.
  Seeds carry selected identity, authored rules, user skills, and clean tracked
  app source while excluding sessions, lived memory, credentials, app data,
  caches, builds, ports, and other machine state.
- Install seeds as independent `origin: starter` wolts with fresh memory,
  private app ports, provenance, collision protection, and pinned Git app
  revisions.
- Add the native `woltspace-seed-review` skill for semantic privacy, secret,
  portability, history, redistribution, and prompt-injection review before a
  seed is shared. A successful review does not authorize publishing.

Colony Seeds are intentionally separate from stateful `backup` and `restore`.
Start with a private seed repository and review all authored content before any
visibility change. See `docs/colony-seeds.md` for the data boundary and install
semantics. The full historical test suite remains outside the release gate while
its environment assumptions are refactored.
