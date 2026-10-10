# Pi harness roadmap

Status: **experimental POC**. The disposable print-mode lifecycle is proven;
the interactive Woltspace TUI/session path is not yet live-certified.

## Chosen first path

- Harness: Pi (`@earendil-works/pi-coding-agent`), initially pinned to 0.87.1.
- Provider: OpenRouter.
- Proven model/default seed: `openrouter/anthropic/claude-sonnet-5`. All tiers
  intentionally share it until atomic harness/provider/model configuration can
  express independently reviewed tier choices. `openrouter/auto` remains an
  optional catalog entry, not the default.
- Integration mode: Pi's interactive TUI inside the existing tmux lifecycle.
  RPC and SDK integration are deferred until interactive lifecycle parity is
  proven; they may later provide structured progress, interruption and events.

## Groundwork now present

- A harness-table entry and command builder with Woltspace-owned session UUIDs.
- A `wpi` wrapper with per-wolt config, auth and session roots.
- Existing `AGENTS.md` and Agent Skills bridges reused.
- A pinned isolated Docker build stage.
- Doctor discovery/auth paths and offline command/model metadata tests.

No OpenRouter credential is bundled and no live-lodge install or restart is
part of this work.

## Disposable POC evidence (2026-09-25)

- Pi 0.87.1 in an isolated Docker stage reached OpenRouter Claude Sonnet 5.
- The initial no-session/no-tools hello returned exactly `hello from Pi`: 624
  input + 8 output tokens, $0.001328 reported cost.
- The `wpi` lifecycle proof created a caller-owned UUID, persisted exactly one
  JSONL session, restarted in a fresh container, resumed that exact UUID, and
  returned the requested second response. First turn: 635 tokens/$0.00131;
  resumed turn: 657 tokens/$0.00137; combined cost: $0.00268.
- Only `OPENROUTER_API_KEY` entered the container. Pi created `auth.json` mode
  0600 containing exactly `{}` (two bytes); the environment credential was not
  persisted. No tools, context files, skills, extensions, templates or themes
  were loaded.
- Containers, candidate images and disposable session data were removed after
  inspection. The live lodge and its sessions were untouched.

## Gates before a live Woltspace session

1. Review the diff and dependency/package payload. Confirm the pinned npm
   artifact and Node runtime on both target architectures.
2. Exercise the interactive TUI through tmux: boot prompt/skill invocation,
   readiness marker if needed, paste/Enter timing, Stop and liveness detection.
3. Keep the owner-controlled OpenRouter credential in the existing secret
   boundary; never commit it or copy it into fixtures/logs.

## First separately approved live proof

In a disposable wolt: launch the interactive TUI, invoke the boot skill,
exercise `notify`, stop the process, resume the exact UUID, and verify
concurrent sessions do not cross. Measure TUI readiness and paste/Enter timing.
Confirm process liveness and vulture behavior. Remove disposable auth, sessions
and image afterward.

## Product follow-ups

- Replace the shared Sonnet 5 seed with reviewed tier defaults only when Woltspace can
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
