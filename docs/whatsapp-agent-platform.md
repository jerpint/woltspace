# WhatsApp Agent Platform connector

The opt-in connector uses Meta's official WhatsApp Agent Platform API at
`https://api.whatsapp.com/agent/v1`. It does not use WhatsApp Business Cloud
API or linked-device automation. The platform is currently creator-only and
messages are not end-to-end encrypted; use it only if those account limits fit.

Create an agent in WhatsApp **Settings > Agents > Create an agent**, open its
chat, and copy its API key from **Chat info > API key**. Configure the key in
the lodge's private `.space/platform/config.json`:

```json
{
  "channels": {
    "whatsapp": {
      "enabled": true,
      "agent_token": "<Agent API key>"
    }
  }
}
```

Or use `ENABLE_WHATSAPP_AGENT=true` and `WHATSAPP_AGENT_TOKEN` in the private
environment. Never put the key in a prompt, log, repository, or public artifact.
Start the lodge through `woltspace start`. The connector runs only in the
platform entrypoint and takes an exclusive local poll lock per API key.

Send `/wolt NAME` to choose the destination wolt, then send text. Replies from
that session return to the same recent WhatsApp participant. Send `/wolt NAME`
again to switch destinations. Only plain text is supported; inbound media gets
an explanatory response and outbound files, voice, and speech are unsupported.

### Commands

WhatsApp uses the same slash command names as Telegram. Text commands replace
Telegram's buttons; no model session is needed for management commands.

| Command | Behavior |
| --- | --- |
| `/start`, `/help` | Introduction, commands and current target; does not reset it. |
| `/wolt` / `/wolt NAME` | List eligible wolts or switch target; next text starts its session. |
| `/new` / `/new NAME` | Immediately create a fresh session for the active/named wolt; previous session stays up. |
| `/sessions` | Active sessions and links through the configured public lodge URL. |
| `/holds` | Held active sessions, reasons and expiry. `/holds release SESSION` releases one. |
| `/idle` | Show lodge-wide idle window. `/idle never`, `/idle 1h`, `/idle 4h`, `/idle 1d` change it. |
| `/apps` | Apps, status and public links. `/apps start NAME` / `/apps stop NAME` manage an existing app. |
| `/kill SESSION` | Request a registered active session stop; clear only this owner's matching active reference. |

The old `wolt NAME` spelling remains a compatibility alias; `/wolt NAME` is
canonical. Unknown slash commands and malformed arguments never reach a model.
Listings are bounded to one text reply and marked if truncated. Management has
the same lodge-wide owner scope as Telegram, but WhatsApp session records for a
foreign account/participant are excluded from listings and session mutations.
Mutations check a fresh authoritative session/app record, not a cached listing.
Session links are built from the configured public HTTPS lodge base, not an
arbitrary record URL or local API address; URL credentials/query tokens are omitted.
The existing lodge access protection still applies to those links.

**Creator trust:** the authenticated official Agent Platform poll is creator-only.
Its first valid fresh text atomically pins the opaque creator participant to the
current account fingerprint before any command/model dispatch. A `user:` prefix
or caller-supplied command is not identity proof. This guarantee is the trust
anchor; if the provider introduces other senders, this enrollment policy must be
reviewed before enabling them. Subsequent foreign senders are ignored. Malformed
or foreign persisted bindings fail closed without overwrite; a rotated key uses
separate binding/routes. An existing same-key installation enrolls on its next
valid fresh provider text, preserving its routing file. No direct `handle` path
can enroll an owner or bypass this check.

Cursor/dedup checkpointing precedes binding persistence and dispatch. A binding
write failure can therefore safely lose that first item; it cannot dispatch it
without ownership. Do not blindly replay an ambiguous management operation.
Provider acknowledgement failure never retries an action or claims that a
successfully created `/new` session failed. These new command behaviors have
offline mocked coverage; no additional live command acceptance is claimed.

### Routine pings through the owner tool (opt-in Codex)

Set `WOLTSPACE_NOTIFY_TOOL=1` in the lodge process environment before starting
it, then start a new WhatsApp Codex session. Codex 0.163+ is required. The
trusted session launcher exposes one stdio MCP tool, `notify_owner(message)`.
It accepts message text only; the lodge supplies the name/creature prefix and
the stored WhatsApp owner destination. No routine shell approval is needed
for this tool. Optional/unconfigured sessions, other adapters and other
harnesses retain their existing notification behavior.

The launcher passes a per-invocation `-c mcp_servers.woltspace_notify={...}`
definition with the absolute bundled worker and Python paths, `-I`, a fixed
local API endpoint, session name and non-secret launch-binding digest. The
allowlist is exactly `enabled_tools=["notify_owner"]`, and the only tool
preauthorization is `tools.notify_owner.approval_mode="approve"`. It sets no
blanket MCP auto mode and changes no shell/network/reviewer policy. Managed
requirements still apply. Configuration parsing alone is not proof of
runtime no-prompt behavior; see the bounded acceptance results below.
`auto` must not be treated as an unconditional grant for a tool that writes.
The exact-tool `approve` override follows the supported
[MCP configuration example](https://learn.chatgpt.com/docs/extend/mcp).
[Managed approval requirements](https://learn.chatgpt.com/docs/app-server)
remain authoritative; a forced prompt is a configuration conflict to report,
not a reason to change the reviewer or answer a dialog automatically.

Installation must be outside the session/wolt writable roots. The launcher
refuses a worker/interpreter inside those roots and checks the effective
definition using the same wrapper, working directory and config stack.
Codex merges nested maps: an existing `env` entry is not cleared by `env={}`.
Unexpected effective command/args/env/cwd/allowlist disables launch with a
fixed configuration error. Do not configure this server globally or add
worker environment overrides. Exposed startup/tool timeouts are also checked.
`mcp get` does not expose `required` or per-tool approval policy; trusted CLI
values and parser controls do not establish their effective runtime behavior.
The named-root guard and tests cover the intended `:workspace` installation,
including denied parent renames and fixed resolved symlink targets. They do
not discover every custom effective writable root. A supported setup must
keep worker/interpreter/config paths and their ancestors outside broader
writable roots; custom profiles may invalidate these assumptions. This relies
on the actual sandbox/installation boundary, not file ownership alone.

The worker has no WhatsApp key and makes only one fixed local notification
request per tool call, without redirects/proxy environment or transport
retries. The backend checks the running session, launch generation, exact
stored route and current account fingerprint; stale bindings are refused
without channel fallback. Admission uses that validated registry snapshot.
Once admitted to the sender, including its rate wait, a call is in flight:
stop/completion, route/key change or fresh spawn/resume preparation rejects
future calls but does not undo an accepted call. It retains its original
destination/account and is never retargeted. Harness EOF ends the worker
after any bounded active attempt. Only text is accepted: final formatted text must fit 4096 characters
and 16 KiB UTF-8; frames are capped at 32 KiB. Concurrent requests are bounded
and provider pacing/order remains shared with the adapter.

The worker's wall deadline is 90 seconds, below the configured 120-second
tool deadline. A queued/provider request can continue after timeout or caller
cancellation; these do not prove cancellation of delivery. Serial stdio
processes a queued cancellation after the active attempt, then refuses further
sends. After an ambiguous send it also refuses all further attempts. This is
worker-lifetime suppression, not durable duplicate protection: restart loses
that state. Do not resubmit through a new worker or the CLI until the prior
outcome is resolved. Requests/errors expose fixed statuses, not provider
responses or route/credential details.

**Trust boundary:** this uses the existing trusted-host lodge API. Session
IDs and launch digests are routing/staleness data, not authentication or
secret capabilities. Unsandboxed same-UID host code can impersonate local
callers; this is not adversarial cross-wolt isolation or an encrypted secret
vault. Provider credentials remain server-side and are scrubbed from agent,
tmux and worker configuration. No new kernel broker is introduced.

If the owner tool is absent, the existing `notify` CLI may require normal
one-time command approval to reach the local API. Load the general notify
skill and honor the current reviewer. Do not enable broad networking or
answer dialogs automatically. The model needs no direct provider access.

The first poll uses explicit `offset=0`, with a persisted activation time to
ignore older retained messages. The `next_offset` cursor is persisted; HTTP 204
keeps the same cursor because it has no body. Message IDs are also persisted
before dispatch. There is an unavoidable crash window: a process can persist an
offset or ID and stop before a session receives the message. Ordinary item-level
dispatch failures are logged and later items in the same batch still run; the
failed item's persisted ID prevents automatic replay because its outcome may be
ambiguous. Malformed items are skipped. Recovery therefore does not claim
exactly-once dispatch. A session-start acknowledgement can also be lost after
the session was successfully created; this does not trigger a misleading
"session failed" reply. The account token's one-way fingerprint
names cursor and routing files, so changing keys does not inherit earlier
participant ownership. The API caps updates polling at 15 requests/minute and
message sends at 12 requests/minute. Explicit HTTP 429 refusals receive a
bounded retry using `Retry-After`; timeouts and other ambiguous send failures
are not retried automatically to avoid duplicate replies.

Offline tests exercise cursor, sender, routing, lifecycle and recovery behavior.
The owner completed a bounded isolated real WhatsApp tool-only acceptance test:
WhatsApp inbound → one real Codex session → `notify_owner` → the exact WhatsApp
nonce reply. The owner received the reply without attachment or approval
interaction. The runner verified the account-bound registry route, reported
one owner endpoint request and zero legacy requests, and reported cleanup passed.

Separate earlier checks exercised a real CLI `notify` reply and a local
mocked-outbound owner-tool Codex session without owner interaction. These are
distinct paths; the tool-only live result establishes the changed path for this
configured account/runtime. It is not a universal approval guarantee, production
endurance/load result, or evidence of general account availability. Managed
requirements remain authoritative. No installed lodge configuration or running
server was changed; the feature remains opt-in, not generally enabled there.
Credentials and private test artifacts stay private.

Keep only one active consumer per key; poll/send coordination is local to the
configured state directory, not distributed across hosts or separate lodges.


References: [WhatsApp Agent Platform Developer Manual, v1 (Aug. 25, 2026)](https://www.whatsapp.com/developer/WhatsApp-Agent-Platform-Developer-Manual.pdf)
