# WhatsApp Agent Platform connector

The text-only WhatsApp connector uses Meta's official Agent Platform API at
`https://api.whatsapp.com/agent/v1`. This is the creator-only Agent Platform,
not Business Cloud API or linked-device automation. Messages are not end-to-end
encrypted. Account availability and provider limits still apply.

The connector is a folder, `container/connectors/whatsapp/`, using the same
local socket protocol as Telegram. The lodge handles commands, the dog, chat
state, session routing and notifications; the connector handles the provider,
creator filtering, polling and text rendering. It adds no provider-specific
branches to the server, launcher, notify CLI or session code.

## Configuration

Set the key and exactly one explicit creator participant id in the private
lodge configuration, `<data root>/.space/platform/config.json`:

```json
{
  "channels": {
    "whatsapp": {
      "enabled": true,
      "agent_token": "<Agent API key>",
      "owners": ["<creator participant id, including user: prefix>"]
    }
  }
}
```

The key can instead come from `WHATSAPP_AGENT_TOKEN`. The bundled connector
also recognizes `ENABLE_WHATSAPP_AGENT` at the platform entrypoint. The
explicit `owners` setting is required in either case; a key alone never
opens a management channel. Obtain the creator id through the provider or
inspect your existing private per-key creator binding locally. Do not paste
keys or private binding files into a model prompt.

Restart the lodge after configuring it. `woltspace status` reports the
connector's state. Do not start a second poller with the same API key.

The folder interface is available in 0.5.25. Install the same folder privately at
`<data root>/.space/connectors/whatsapp/`, without modifying the installed
package. Copy all four files together; enable it through configuration (local
connectors do not use the bundled environment enable flag). Never run a local
copy alongside a bundled connector with the same key. See
[the connector guide](connectors.md).

## Chat and replies

The lodge supplies its command menu, including `/help`, `/wolt`, `/new`,
`/sessions`, `/holds`, `/idle`, `/apps` and `/kill`. `@dog` addresses the lodge
dog. Pickers render as numbered text choices; send a number while that picker
is open. Pickers expire after five minutes and disappear on connector restart.
When the lodge holds your first message while you choose a wolt, the numbered
picker stays active through its follow-up confirmation. Send a number to deliver
the held message. If the picker expires or the connector restarts, send
`/wolt <name>` within the lodge's ten-minute hold window. This flow uses
the shared held-message feature in 0.6.

A successfully submitted selection is consumed once, before its outcome is
acknowledged. A failed acknowledgement never reruns the selection.

The shared command parser recognizes only registered commands. Leading spaces,
doubled slashes and unknown slash names remain ordinary text, matching other
connectors. This differs from the earlier draft adapter's command parser.

Session reply instructions use generic `notify --to whatsapp:CHAT_ID`. The
manifest declares one `chat_id` consisting of the existing key fingerprint,
a dot, and the URL-safe base64 encoding of the full participant id. The lodge
persists that field on sessions, so implicit session notifications and explicit
replies use the same destination and session footer. Opaque creator ids retain
their complete value, including embedded colons.
The connector refuses a different account or participant and sends only to
its configured, persisted creator. Rotating the key requires a new chat route;
old sessions are never silently retargeted. The lodge independently checks
all inbound senders against its own configured owner list.

Only text up to 4096 characters is sent. Files, voice and speech are refused.
Inbound attachments receive a text explanation after creator enrollment.
Long lodge replies are truncated and their picker is disabled. The 4096-character
limit includes any session footer added to a notification. Notifications use the
shared connector path and its normal runtime approval policy.

When the optional inbox is enabled, the lodge queues the owner's messages and
carries this chat route into a session it starts. The shared conversation log,
when enabled, records notification attempts and outcomes.

## State, delivery and migration

The connector keeps private state under `<data root>/.space/whatsapp/`:
per-key cursor/dedup, creator binding, exclusive local poll lock, and rolling
request budgets. The first valid fresh provider text must match the configured
creator before its account binding is persisted and any event is dispatched.
The authenticated creator-only poll is still the identity trust anchor;
participant syntax alone is never authentication. Malformed, foreign or
nonprivate creator bindings fail closed. Supported participant ids are
printable ASCII `user:` ids of at most 128 characters, within the socket's
sender limit.

The existing draft adapter's same-key creator binding and cursor formats are
retained. The previous account/participant route now fits in one `chat_id`.
For a preview lodge using the earlier route, choose the wolt and start a fresh
WhatsApp session. Existing session records, old chat histories and private
`routes-*.json` files retain their old route. Old explicit destinations are
refused. An implicit notification from a session without a usable route may
follow the lodge's default channel, so use a newly bound WhatsApp session for
the test. Stop the old adapter before enabling this folder; both use the same per-key local lock
when pointed at the same data root. Remote hosts are not coordinated.

Provider cursor/dedup checkpointing happens before dispatch, so a crash in that
window can lose an event. The socket client resends unreceipted events during
reconnection; the lodge deduplicates event ids before dispatch. Its accepted
receipt is one dispatch attempt, not a promise of completed command handling.
The connector's pending socket events and numbered pickers are in memory.

Polling is limited to 15 requests per rolling minute and sends to 12. Explicit
429 responses get at most two retries, each with a bounded delay. Timeouts,
network errors and ambiguous server errors are never automatically retried as
sends. The caller must resolve an ambiguous outcome before repeating a message.
There is no exactly-once delivery guarantee.

Offline tests cover manifest discovery and opt-in launch, sender/account
checks, credential rotation, checkpoints, commands, numbered pickers, and real
socket receipts and deduplication. The shared first-message flow is tested with
the inbox off and on, including expired-picker command recovery, session route
persistence, implicit and explicit notifications, history, and footer length
checks. Provider HTTP and agent execution are mocked. Live provider acceptance
requires a private smoke test.

## Private smoke test

Use a throwaway lodge for an unreleased 0.6 build, with its own data root and
authentication directories. The current port targets upstream main at
`b7e8370`. That build includes the shared held-message behavior. The package's
displayed version is `0.6.0rc4`. Keep different release lines on separate data
roots because 0.6 migrates the lodge database.

1. Copy all four connector files into the test lodge's
   `.space/connectors/whatsapp/` directory. If using a build that already
   bundles WhatsApp, configure that bundled copy.
2. Configure one dedicated Agent Platform key and its exact creator id using
   the configuration above. Keep the key private. Ensure one process uses it.
3. Start the test lodge and confirm WhatsApp is connected in
   `woltspace status`.
4. Send `/help` and confirm the shared command list arrives.
5. With two test wolts and no active wolt, send a short message. Choose a wolt
   by number and check that the original message reaches it once.
6. Ask for a short reply and confirm the notification and its session link
   arrive in WhatsApp. Test `/sessions` and a second numbered choice.
7. Repeat the first-message flow with the inbox enabled if you intend to use
   that feature. Test `/wolt <name>` after the local picker expires.

To end the trial, disable `channels.whatsapp.enabled` and stop the test lodge.
Keep its private data for inspection. Key rotation creates a new route and
requires a fresh WhatsApp-bound session.
