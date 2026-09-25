# Pi harness roadmap

Status: **experimental POC**. The disposable print-mode lifecycle and a
containerized interactive-TUI/IWCL roundtrip are proven; native supervisor
registration and browser attachment are not yet live-certified.

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
- A follow-up disposable wolt loaded the real copied-skill tree through
  `.agents/skills`, expanded `/skill:woltspace-start-chat`, read the Slack mode
  and its own identity, and successfully invoked the real `notify` binary into
  the originating Slack thread. A fresh process then reopened the exact same
  session and expanded `/skill:woltspace-notify`, producing a second successful
  Slack notification while retaining its probewolt identity.
- The skill/conversation proof used five assistant model steps and cost
  $0.0363831 total, calculated by summing every assistant message's usage. This
  matters for tool loops: the final assistant message's cost is only the last
  request, not the whole turn. The persisted file was one 29,739-byte JSONL;
  both skill expansions and both successful notify results are recorded there.
- The disposable skill wolt was moved to Trash (recoverable); its container and
  image were removed and zero matching Docker remnants remained.
- A subsequent disposable run launched Pi's real interactive TUI on a PTY. Pi
  discovered `woltspace-iwcl`, sent a heredoc message through the live lodge to
  the exact current n00b session, accepted an IWCL-shaped inbound prompt in the
  same TUI, and sent the exact reply back through the lodge. Both deliveries
  were observed with the `probewolt` identity and exact source session.
- The interactive proof used 12 assistant steps and $0.0521424 total. Its
  first attempt exposed a packaging-fixture detail: the isolated `pi` Docker
  build stage contains Node and Pi but not Python, so the Python control CLI
  cannot run there until Python is supplied. The composed Woltspace runtime
  must be the acceptance target; a bare harness stage is insufficient for
  connector/IWCL tests.
- The PTY accepted normal typed prompts and multi-line IWCL prompt injection,
  and clean Ctrl-D exit printed the exact resumable session command. This is
  strong evidence for the tmux path, but does not yet prove Woltspace session
  registry creation, browser attachment, Stop, or vulture behavior.

## Gates before a live Woltspace session

1. Review the diff and dependency/package payload. Confirm the pinned npm
   artifact and Node runtime on both target architectures.
2. Exercise the interactive TUI through the actual Woltspace tmux supervisor:
   registry/browser attachment, readiness timing, Stop and liveness detection.
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
