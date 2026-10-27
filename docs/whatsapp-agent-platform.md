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
successfully created session failed. Creator enrollment has offline mocked coverage.

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

Offline tests exercise cursor, ownership, sender, routing, lifecycle and recovery.
The existing notify CLI may need normal command approval to reach the local API.
No optional owner tool or live-test harness is included in this layer.

Keep only one active consumer per key; poll/send coordination is local to the
configured state directory, not distributed across hosts or separate lodges.


References: [WhatsApp Agent Platform Developer Manual, v1 (Aug. 25, 2026)](https://www.whatsapp.com/developer/WhatsApp-Agent-Platform-Developer-Manual.pdf)
