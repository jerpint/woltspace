# Pi harness roadmap

Status: **experimental groundwork only**. The adapter is not live-certified and
must not be presented as a working harness until the gates below pass.

## Chosen first path

- Harness: Pi (`@earendil-works/pi-coding-agent`), initially pinned to 0.87.1.
- Provider: OpenRouter.
- Initial model selector seed: `openrouter/auto`. It avoids pretending that a
  fast-moving routed-model catalog is a stable tier mapping. Explicit
  `openrouter/<vendor>/<model>` pins are structurally supported but unverified.
- Integration mode: Pi's interactive TUI inside the existing tmux lifecycle.
  RPC and SDK integration are deferred until interactive lifecycle parity is
  proven; they may later provide structured progress, interruption and events.

## Groundwork now present

- A harness-table entry and command builder with Woltspace-owned session UUIDs.
- A `wpi` wrapper with per-wolt config, auth and session roots.
- Existing `AGENTS.md` and Agent Skills bridges reused.
- A pinned isolated Docker build stage.
- Doctor discovery/auth paths and offline command/model metadata tests.

No OpenRouter credential is bundled. No login, model request, Pi session,
container build, live-lodge install or restart is part of this groundwork.

## Gates before the first model call

1. Review the diff and dependency/package payload. Confirm the pinned npm
   artifact and Node runtime on both target architectures.
2. Build a disposable image and run only `pi --version`, `pi --help`, and
   filesystem-wrapper probes. Assert no credential or provider traffic.
3. Provide an owner-controlled OpenRouter credential through the normal secret
   boundary; never commit it or copy it into fixtures/logs.
4. Select one explicit low-cost model and a spend limit for the first call.

## First separately approved live proof

In a disposable wolt: authenticate OpenRouter, spawn one session, invoke the
boot skill, exercise `notify`, stop the process, resume the exact UUID, and
verify concurrent sessions do not cross. Measure TUI readiness and paste/Enter
timing. Confirm process liveness and vulture behavior. Remove disposable auth,
sessions and image afterward.

## Product follow-ups

- Replace `openrouter/auto` with reviewed tier defaults only when Woltspace can
  atomically validate harness/provider/model combinations.
- Expose provider identity separately from model identity rather than parsing a
  slash-delimited string throughout the product.
- Decide whether native-host Pi is acceptable. Pi has no built-in filesystem,
  process, network or credential sandbox; the launcher owns that boundary.
- Evaluate RPC only after terminal parity. It is promising for structured
  progress and Stop, but should not leak Pi-specific lifecycle into the common
  harness abstraction.
- Treat Pi packages and extensions as executable code requiring review; never
  auto-install them from a model suggestion.
