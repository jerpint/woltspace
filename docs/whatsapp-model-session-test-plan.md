# Isolated WhatsApp model-session test plan

**Status: owner-observed bounded tool-only acceptance passed.** A real
WhatsApp inbound created one Codex model session; its `notify_owner` call
returned the exact nonce to WhatsApp. The owner received it without attachment
or approval interaction. The runner reported the account-bound registry route,
one owner endpoint request and zero legacy requests, and cleanup passed. No repeat is needed.

Separately, earlier checks passed the real CLI reply path and the local
mocked-outbound owner-tool runtime. These are distinct evidence: one live
configured tool result is not a universal managed-policy, load/endurance or
account-availability guarantee. No installed lodge configuration was changed;
the tool remains opt-in for WhatsApp Codex 0.163+ sessions.

The runner is `scripts/whatsapp_model_session_test.py`. Its current tool-only
option is `--run --harness codex --notify-owner-tool`; it requires a real TTY
and typed confirmation, then only the printed WhatsApp selection/challenge.
Use `whatsapp-owner-test.md` for the current reviewed procedure. A tool prompt
is a conflict to report, not a dialog to answer or a reason to change policy.

The detailed CLI/diagnostic sections below describe the separate default CLI
mode and optional troubleshooting. Its attachment/normal shell-approval steps
are not required for the tool-only path. Any future run needs review; never
repeat an ambiguous send. Stages one/two in `whatsapp-owner-test.md` exercise
transport and registry routing without creating a model session.

## Isolation boundary

- Use a fresh directory under `.whatsapp-owner-test/` with mode `0700` for the
  test `WOLTS_DIR`, one synthetic `wa-test` wolt, session registry, logs, and
  temporary server state. Do not copy real wolt directories or their secrets.
- Run the source-checkout `server.app:app` in the runner process with its
  lifecycle disabled, bound only to `127.0.0.1` on a dynamically allocated
  port. Reserve and pass an already-bound socket so the selected port cannot
  race with another process. This keeps the real `/notify/owner` (tool) or
  `/notify` (CLI) route while skipping
  tunnel, connector, watcher, and lodge lifecycle startup. Set `WOLTSPACE_API`
  and `PORT` to that local endpoint; never bind port 7777 or change the lodge's
  running server.
- Set `WOLTSPACE_DIR` to the reviewed checkout and both wolt directory
  variables to the synthetic test root. Disable all channel connectors on the
  isolated server. Run the WhatsApp poller as the only WhatsApp API-key
  consumer.
- Give the poller the key from the reviewed private key file in process memory.
  Do not put it in argv, output, logs, or the session environment. For the
  server's notify sender, use an in-process secret lookup backed by that
  private file; do not duplicate the key into `.env` or a tracked artifact.
  Server output goes to a private `0600` temporary log that is removed during
  successful cleanup.
- Set `TMUX_TMPDIR` to a separate per-run directory under the wolt owner's
  private `.whatsapp-model-tmux/` directory. Both directories are mode `0700`,
  checked for current-user ownership, and the exact `tmux-UID/default` socket
  pathname is checked against Linux's 108-byte `sockaddr_un.sun_path` bound
  before the WhatsApp key is read or any API poll can start. This short path
  avoids nesting the socket under the long source checkout and test root. The
  finalizer removes only that run's socket directory after confirming the
  private server is stopped; if cleanup is uncertain, it preserves the run
  root and socket directory for scoped cleanup. Use an already
  owner-authorized harness login through the existing supported
  authentication path (for example, the configured `CODEX_HOME` or
  `CLAUDE_CONFIG_DIR` path carried by the session runtime). Do not copy,
  print, or inspect authentication files. The test runner should pass only the
  selected harness path through the existing session environment allowlist;
  it should not inherit unrelated owner environment variables. If no supported
  authorized harness login is configured, stop and report that prerequisite.
  The test starts a fresh tmux server there, removes inherited `TMUX` state,
  and verifies the socket path and WhatsApp-token environment before accepting
  the model reply.

## Single-message flow

### WhatsApp model-session flow

After review, invoke it from this checkout with the owner-selected Codex
harness and its existing authorized login:

```sh
.venv/bin/python scripts/whatsapp_model_session_test.py --run --harness codex
```

The command refuses non-interactive input and asks for the exact word `MODEL`
before reading the private WhatsApp key. It prompts for two WhatsApp messages:
the exact wolt selection and a generated nonce challenge.
Have a second terminal ready. After the session's account-bound route and
private runtime have been verified, the runner prints an exact scoped tmux
attach command, `Approval needed`, and a fresh 300-second observation budget.
Use only this newly printed attach command; previous temporary sessions are
gone. If the normal reviewer shows the intended `notify` command request,
the owner may approve it **once**, without a persistent rule. Do not answer
unrelated prompts. Feature authorization does not replace runtime approval.
Escalation applies to the command and children, not an exact destination port.
The runner never answers dialogs or changes network, reviewer or sandbox
configuration. Detach with Ctrl-b then d; Ctrl-C in the runner triggers scoped
cleanup. There is no automatic retry after an ambiguous send.

The selection/challenge phase retains its own 300-second limit. Time spent
waiting for those messages does not consume the subsequent owner approval
budget: it starts once the exact private session is verified for attachment.
This is **runtime attachment readiness**, not evidence of model readiness.
Codex's separate readiness field remains unsupported/unknown without a marker.
While no notify attempt is observed, the runner repeats a pending status at
the next polling iteration after 15 seconds (a provider long poll can delay
that observation). If its budget expires, it visibly reports
`approval_not_completed`, not a claim of approval denial, and cleans up.

The real adapter still creates the session through the normal supported path.
Only this test's start callback appends the fixed normal-approval instruction
to the model prompt, without changing the owner's exact challenge or the
one-nonce outbound gate. Separately, the source WhatsApp start-chat mode now
explicitly loads `woltspace-notify` before the first reply. Before key access,
preflight verifies the delivered WhatsApp mode and notify instructional body
match this checkout (delivery intentionally renames skill frontmatter). Thus
the general local-API guidance is also present in actual WhatsApp bootstrap.
The selected harness must already have an authorized `CODEX_HOME` (or
`~/.codex`) directory. Before reading the WhatsApp key or polling, the runner
checks that the Codex executable and `wcodex`, `run-session.sh`, `session-reg`
and `notify` are available; runs Codex `--version` and `--help` with a
disposable `HOME` and without `CODEX_HOME`; seeds the actual platform skills
into the synthetic wolt; and verifies the Codex start-chat/notify skills and
the session's `notify` path resolve. This is an offline executable/bootstrap
check only. It does not read or validate auth file contents or prove the
account is signed in. The runner creates a private per-run `CODEX_HOME` and
symlinks its `auth.json` to the selected existing login directory. Codex
session/config writes therefore stay in the run root; no auth file is copied
or opened by the runner. The supported `CODEX_HOME` variable is passed through
the actual session runtime.

On failure, the terminal reports only a fixed `stage` and allowlisted
`category`. Stages are `preflight`, `server_health`, `inbound_selection`,
`selection_reply`, `challenge_dispatch`, `session_spawn`, `harness_boot`,
`session_ack`, `notify`, and `cleanup`; each has a `pending`, `passed`,
`failed`, or `not_reached` status. Categories are fixed values such as
`executable_missing`, `version_probe_failed`, `help_probe_failed`,
`bootstrap_missing`, `auth_or_config`, `session_not_started`,
`notify_rejected`, `notify_ambiguous`, `run_timeout`, and `cleanup_failed`.
The failure record includes only the harness name, primary stage/category and
those statuses. It never contains a prompt, participant/session ID,
credential, exception text, or pane/log contents. Raw runner and session boot
logs are removed after cleanup attempts.
Schema 3 retains the fixed `notify_attempt` state (`not_seen`,
`dispatch_wait`, `reserved`, `route_verified`, `send_in_flight`, `sent`,
`rejected`, or `ambiguous`); it distinguishes no notify invocation from one
that reached the sender but did not finish before timeout. It separately
records `harness_process` (`not_checked`, `present`, `absent`, `unknown`),
`harness_readiness` (`not_checked`, `ready_marker_seen`, `prompt_detected`,
`not_ready`, `unknown`, `unsupported`), and `boot_symptom` from a fixed
allowlist. Process presence is not harness readiness. Codex currently has no
configured versioned TUI-ready marker, so its readiness is `unsupported`; a
running Codex process alone does not mark `harness_boot` passed.

New records add only fixed `notify_rejection` (`none`, `dispatch_timeout`,
`payload_mismatch`, `route_unverified`) and `notify_payload_shape` (`not_seen`,
`exact`, `exact_plus_terminal_lf`, `other`) enums. The terminal includes these
classes after a notify attempt, so the owner need not share private logs.
Route verifier exceptions map to `route_unverified` without retaining exception
text. The first attempt's evidence remains unchanged after a duplicate attempt
is refused. No payload, identifier, nonce, exception or arbitrary field is
retained. The strict validator also accepts historical schema-2 records with
exactly their old fields; missing evidence is not retrospectively inferred.

The runner classifies captured boot symptoms in memory as `auth_or_config`,
`bootstrap_missing`, `trust_or_approval`, `waiting_for_input`, or
`none_detected`; capture failure is `capture_unavailable`. Source text is
never printed or stored. Concrete directory-trust/onboarding or auth/bootstrap
symptoms stop the run. Tool approval may wait for the owner; broad approval or
input-wait text stays observable with readiness unknown because it can be
echoed instructions. The runner does not interpret that text as a real dialog
or answer it. A missing notify attempt at the deadline is inconclusive about
whether approval was displayed or denied.
After verified cleanup, the private run directory retains
only the mode-`0600` `failure.json`. If cleanup cannot be verified, it retains
`.cleanup-required` and private cleanup state, removes `runner.log`, and writes
the same sanitized record.

Offline fault-injection coverage exercises no notify invocation, route
rejection, a sender that remains in flight, completed send, and synthetic
auth/bootstrap/trust/approval/waiting pane text. These checks validate the
diagnostic states and redaction only; they do not diagnose a prior live run.

The test allows one exact `🦦 wa-test: <nonce>` payload, or that exact payload
plus **one terminal LF** from a literal heredoc, from the selected
participant after checking the session registry, account fingerprint, and
private tmux socket. If the model replies while the inbound handler is still
saving its route, the notify request waits asynchronously for at most 45
seconds for dispatch completion, then verifies the saved route and fails
closed if it is absent. The notification sender retains its bounded HTTP 429
handling (up to three attempts, honoring `Retry-After`). The runner does not
retry an ambiguous timeout or any other send failure; the attempt is reserved
before network I/O and all later notify attempts are rejected.
The original message bytes are passed to the sender unchanged; this does not
strip arbitrary whitespace or alter the production notify CLI. Leading or
trailing spaces, additional blank lines, CRLF, extra text and a wrong nonce
remain refused. Offline tests exercise the actual quoted-heredoc shell command,
real CLI and local mocked route for valid and invalid variants, including an
ambiguous sender outcome with no retry.

An earlier live test retained only a generic rejection, so its exact cause
remains unknown. Independently, source-only reproduction established that the
required heredoc preserves its terminal LF while the previous test gate
expected none. The correction addresses that concrete mismatch; it does not
prove that it caused the earlier rejection or establish a successful live reply.

1. Create only the synthetic test wolt and start the isolated server and its
   private tmux server. Confirm the API endpoint is the selected loopback port
   and that it is not 7777.
2. Start the adapter with a fresh activation timestamp and explicit offset 0.
   The owner sends `/wolt wa-test` and receives the adapter's selection
   confirmation. Then they send one unique harmless request asking the test
   wolt to reply with the same nonce and no other action. The runner accepts
   only these two exact inbound texts from that same participant; other
   messages are left undispatched and stop the test.
3. Verify the isolated session registry records the WhatsApp participant and
   account fingerprint; verify the new tmux process has no
   `WHATSAPP_AGENT_TOKEN`; verify the adapter's selection confirmation and
   one nonce reply through the real notification boundary. In tool-only mode,
   require one owner endpoint request and zero legacy requests without attachment/approval interaction.
   For the separate CLI mode only, use the printed private attach command and
   normal one-time command approval if prompted. Do not substitute CLI for an
   unavailable/refused owner tool or retry an ambiguous send.
4. Stop the exact test session, private tmux server, isolated API server, and
   poller. Remove the temporary data root and logs. Keep the credential file
   only until the owner runs the documented cleanup command. If cleanup cannot
   verify the private tmux socket, retain the private run directory with a
   `.cleanup-required` marker and report that cleanup needs attention.

## Stop conditions

Do not run this plan if an owner-authorized supported harness login is
unavailable, the isolated API endpoint cannot be confirmed, or the tmux socket
cannot be proven private. Never use a real owner task as the test prompt, and
do not retry an ambiguous WhatsApp send until the owner checks the chat.
