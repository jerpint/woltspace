# Owner-operated WhatsApp test

This checkout includes an opt-in test for the official WhatsApp Agent Platform.
It has two isolated stages. Stage one checks real WhatsApp polling and sending
with a mocked session handler. Stage two sends one inbound test message through
the real session registry and `server.notify` sender, but still uses no live
Woltspace server or model session.

## Requirements and commands

Use Python 3.11 or newer. The runner uses the checkout's Python sources and
standard library for stage one. Stage two needs the normal Woltspace runtime
dependencies; run it with the checkout-local `.venv` after `uv sync --extra test`.
Both stages require network access to `api.whatsapp.com` after you enter a key.

From the checkout root, start stage one:

```bash
python scripts/whatsapp_owner_test.py
```

The script requires a controlling terminal at `/dev/tty`, asks for explicit
confirmation, then uses `getpass` so key entry is hidden. It writes the key
only to `.whatsapp-owner-test/agent-key` (directory mode `0700`, file mode
`0600`). That directory is gitignored. The key is not placed in argv,
environment variables, output, logs, or shell history. The runner reads it
inside a child process and holds it only in process memory while the test runs.
It does not start a Wolt session, agent, tmux server, or lodge server.

After the isolated poll starts, send `/wolt wa-test` to the WhatsApp Agent and
wait for the selection confirmation. That is one expected outbound message.
Then send the one-time `wa-check-…` phrase printed by the local runner. The
single test message is dispatched to the mocked session handler, which returns
one fixed reply through the real WhatsApp Agent API. The runner prints no
participant ID or inbound message text. It stops after that reply, on timeout,
or when you press Ctrl+C. Stage one proves the Agent API transport and adapter
routing with mocked session dispatch; it does not prove real model execution or
`server.notify` delivery.

For stage two, first complete stage one so the key is stored. Then run:

```bash
.venv/bin/python scripts/whatsapp_owner_test.py --notify-path
```

After the confirmation prompt, send the exact one-time phrase shown by the
runner in WhatsApp. The runner creates a synthetic session record in private
temporary state, then invokes the real `server.notify.send_notification` path
and WhatsApp sender for one canned reply. It tests registry/account binding,
notify routing, rate limiting, and the official send API. It still does not
launch a model session or touch the installed server or port 7777.

The API client may perform bounded retries after an explicit HTTP 429, using
the server's `Retry-After` value. It does not retry an ambiguous send timeout.
If a send times out, check WhatsApp before running another test because the
outcome may be ambiguous. Each one-time phrase reserves a single outbound
attempt before the send starts, even if a duplicate update arrives in the same
poll batch.

## Cleanup

The temporary state is removed when each stage exits. The credential remains
until you remove the private test directory with:

```bash
python scripts/whatsapp_owner_test.py --cleanup
```

This removes the saved key and any remaining private test files. Do not copy
the key into chat or a command line. If the checkout is deleted, the private
gitignored directory is deleted with it.

## Real model-session owner-tool test (completed bounded acceptance)

The owner completed this reviewed tool-only test and received the real nonce
reply in WhatsApp without attachment or approval interaction. The runner
reported one owner endpoint request, zero legacy requests, an account-bound
registry route, and cleanup passed. No repeat is needed. This is bounded
acceptance for the tested runtime/account, not a universal managed-policy or
reliability guarantee. It does not enable the feature in the installed lodge.

For a separately reviewed future use, from a checkout with runtime dependencies:

```bash
.venv/bin/python scripts/whatsapp_model_session_test.py --run --harness codex --notify-owner-tool
```

Type `MODEL` at the local confirmation. The existing saved key is read only by
code, never displayed or placed in model/worker environments or configuration.
In WhatsApp, follow the printed `/wolt wa-test` selection and fresh exact challenge.
No second terminal, session attachment or approval interaction is required.
Do not reuse an earlier nonce. The runner requests only `notify_owner(message)`;
it rejects the legacy CLI endpoint before sender access, rather than silently
falling back. A concrete tool approval prompt fails this mode; do not answer it
or change policy. Report the fixed failure category for review.

Success requires one owner endpoint request, zero legacy requests, the exact
nonce payload, and the real account-bound registry route/private runtime. The
one-attempt gate is reserved before provider I/O; no ambiguous outcome is
retried. The full 300-second observation budget starts after the session/runtime
is verified (this is not a model-readiness claim). Cleanup stops only the private
test session/server/tmux and reports `Model test cleanup=passed.` on success.
The installed lodge and port 7777 are unchanged. A selection confirmation and
session acknowledgement are expected control messages in addition to the nonce.

Without `--notify-owner-tool`, the prior CLI/normal-approval diagnostic remains
available; it is a separate path and prints attachment instructions. Prior real
CLI acceptance does not establish real-provider acceptance of the new tool path.
The tool path separately passed a local mocked-outbound Codex check without
owner interaction and the real-provider acceptance described above. Do not
conflate the earlier CLI result or local mock with the real tool-only result.

## Fixed failure diagnostics

Future private model-run failure records use schema 4 (the reader also accepts
unchanged schemas 2/3). Control replies record only `selection`/`session_ack`,
whether the API function was called, a valid HTTP status if available, documented
numeric provider code `100` if present, and an exception category: timeout,
connection, terminal configuration, rate limited or other. No body, exception
text, URL, header, participant, prompt or credential is retained.

`call_attempted=true` means entry to the API wrapper, including its rate/lock
wait; it does not prove provider submission. `submission=unknown` includes
ambiguous timeout/connection outcomes. `http_response_received` establishes an
HTTP response, not successful delivery. These facts cannot retroactively identify
the cause of an earlier generic selection-reply failure. Do not replay a nonce
or retry a potentially delivered message without review.

Cleanup records independent API-server, listener, session, tmux and finalization
outcomes. A validated absent socket counts as `tmux=not_needed` only when the
runner positively knows no session-start dispatch was requested. The start flag
is set before dispatch, including calls that throw or have ambiguous outcomes;
default/unknown is not never-started. For a started/unknown runtime, a missing
socket might have been unlinked while the server lives, so cleanup fails closed.

The runner kills only the exact verified private server before registry stop/
stamp; `tmux=passed` retains successful server-kill plus disappearance/stop
confirmation. A registry `stopped` result alone is insufficient. The existing
finalizer preserves the root whenever aggregate cleanup fails. Exact private
owned 0700 base/run/socket-parent directories and node ownership/type are checked.
Permission, foreign ownership, symlink or unverified states retain evidence. Report the fixed failure line; never dump private raw logs or
auth/key files. Diagnostic changes alone are not a fix for an unknown provider
failure and do not authorize another live attempt.
