# Pi harness evaluation — 2026-09-25

This note records the first bounded evaluation of Pi as a Woltspace harness.
It separates what was observed from what remains product work. All model calls
used disposable containers, an environment-only OpenRouter credential, and a
combined measured model cost below $0.10.

## Why Pi is interesting

Pi exposes the seams Woltspace needs from a harness:

- an interactive terminal UI suitable for the existing tmux lifecycle;
- caller-supplied session UUIDs and exact session resume;
- provider/model selection, including OpenRouter;
- `AGENTS.md`/`CLAUDE.md` context and Agent Skills discovery;
- JSON, RPC, and SDK modes for possible later structured integration.

The first integration deliberately uses the terminal path. RPC and SDK modes
remain deferred until normal session lifecycle behavior is proven.

## Distribution caveat

Pi is a Node CLI distributed through npm as
`@earendil-works/pi-coding-agent`. The candidate container pins version 0.87.1
and installs it into `/opt/pi` with npm. The final Woltspace image also contains
Python and the Woltspace control CLI, so skills such as IWCL and notify work.

A Python wheel cannot bundle an npm-installed executable into a plain
`uv tool install woltspace` by itself. Native one-line distribution therefore
remains unresolved: Woltspace must either manage a compatible Node/npm runtime,
use an upstream standalone Pi artifact if one becomes suitable, or make the
dependency an explicit setup step. The experimental registry entry must not be
presented as seamless native installation until that decision is made.

The isolated `pi` Docker build stage contains only Node and Pi. It rendered the
TUI but could not run Woltspace's Python control client. Testing against the
composed runtime fixed that fixture error and is the correct acceptance shape.

## Proof sequence

### Minimal provider call

Pi 0.87.1 called `openrouter/anthropic/claude-sonnet-5` with tools, session
persistence, context, skills, extensions, templates, and themes disabled. It
returned the exact requested text `hello from Pi` using 624 input and 8 output
tokens at a reported cost of $0.001328.

### Session ownership and resume

The `wpi` wrapper created caller-owned session UUID
`11111111-2222-4333-8444-555555555555`. A fresh container reopened the same
single JSONL session and produced the requested second response. The two turns
cost $0.00268 combined. Provider, model, and persisted UUID matched exactly.

### Skills and Slack notification

A disposable `probewolt` discovered the copied Woltspace skill tree through
`.agents/skills`, expanded the Slack boot skill, read its identity, and invoked
the real `notify` binary through the existing lodge API. A fresh Pi process
resumed exact UUID `22222222-3333-4444-8555-666666666666`, expanded the notify
skill, retained its identity, and sent a second Slack message. Five assistant
steps cost $0.0363831.

The container held no Slack token. Only the existing Woltspace `/notify`
boundary communicated with Slack.

### Interactive TUI and IWCL

Pi's real interactive TUI ran on a disposable PTY with exact UUID
`33333333-4444-4555-8666-777777777777`. It discovered `woltspace-iwcl`, sent
an exact heredoc message through the live lodge to a pinned n00b session,
accepted a multiline IWCL-shaped return prompt in the same TUI, and delivered
the requested response back through the lodge.

Normal typed prompts, multiline paste, Enter submission, tool execution, and
clean Ctrl-D exit worked. Exit printed the exact session-resume command. Twelve
assistant steps cost $0.0521424. This strongly supports the terminal integration
shape but does not prove Woltspace supervisor registration or browser attach.

## Chinese-model comparison

Four models each received a distinct disposable wolt home and the same Pi TUI
task: discover the IWCL skill and send one exact message to one exact session.
Elapsed time is measured from the persisted user message to the final assistant
message; costs are the sum of every persisted assistant usage record.

| Model | Result | Time | Assistant steps | Cost |
| --- | --- | ---: | ---: | ---: |
| DeepSeek V4 Pro 0813 | pass | 5 s | 3 | $0.00416042 |
| DeepSeek V4.1 Flash | pass | 6 s | 3 | $0.000984162 |
| GLM 5.3 Flash | pass | 6 s | 3 | $0.00163805 |
| MiMo V2.6 Pro | qualified failure | 19 s | 5 | $0.0026587674 |

GLM did not support thinking-off and Pi honestly displayed its minimum `low`
setting. It otherwise completed the task exactly once.

MiMo understood the task and found the correct skill, but generated different
random opening and closing heredoc delimiters. The malformed command delivered
the requested text plus a stray delimiter marker. MiMo noticed the error,
retried with matching delimiters, and caused a duplicate delivery. It was then
interrupted. Transport was healthy; the failure was exact-once tool discipline.

The four comparison runs cost $0.0094413994 combined.

## Security and cleanup evidence

- `OPENROUTER_API_KEY` entered containers only as an environment variable.
- Every Pi-created `auth.json` was mode 0600 and contained exactly `{}`.
- Persisted state contained no OpenRouter credential marker.
- Disposable wolt homes, containers, images, and session data were removed
  after measurement.
- No live Woltspace code, configuration, process, or installed package changed.
- The only live side effects were explicitly authorized Slack and IWCL proof
  messages through existing lodge APIs.

## Product finding: Woltspace conformance evals

MiMo's result exposes a useful distinction: a general benchmark may reward
self-correction, while Woltspace must fail a task that causes duplicate external
effects. A future harness-neutral conformance runner can assert:

- correct wolt and session identity;
- intended skill discovery without unrelated work;
- exact heredoc preservation and exactly-once delivery;
- exact target and payload routing;
- no unauthorized spawn or scope expansion;
- inbound IWCL reply behavior;
- exact session persistence and resume;
- prompt interruption and process liveness;
- absence of persisted credentials;
- measured latency, tokens, cost, retries, and side effects.

This evaluates whether a model/harness pair behaves as a trustworthy Woltspace
collaborator, not merely whether the model can solve a coding benchmark.

## Remaining gates

1. Register Pi through the actual Woltspace supervisor and attach the browser
   TUI to the live tmux pane.
2. Verify readiness timing, interruption/Stop, vulture behavior, dead-session
   recovery, and concurrent-session isolation.
3. Decide and test native Node/npm distribution for the one-line uv experience.
4. Add deterministic conformance fixtures that distinguish model behavior,
   harness behavior, and transport behavior.
5. Review the executable npm dependency and multi-architecture container
   artifact before any merge, release, or live installation.

