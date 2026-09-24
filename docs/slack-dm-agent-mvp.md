# Slack owner-DM MVP handoff

## What this slice ships

Slack is a native supervised connector with the same start, stop, health,
bounded-restart and orphan-reaping lifecycle as Telegram. It accepts only
`message.im` events from one exact configured Slack member ID. Channel
mentions, multi-person DMs, bot/subtype events, malformed owner configuration
and duplicate events are rejected before history, attachment, model or session
work. Existing persisted Slack thread-to-session ownership remains the routing
model.

Every fresh top-level text DM starts a new conversation by showing one Block Kit
`static_select` plus an exact-name text fallback. The original text is held in a
private, size-capped, ten-minute pending record. A valid owner selection claims
it atomically, removes the stored plaintext, and delivers it exactly once to a
new session for that wolt. Duplicate, stale, expired, forged or ambiguously
recovered claims never replay it. A prior selection is displayed only as a
convenience; it does not bypass the picker. Historical owned threads remain
pinned to their original session. Attachments receive an explicit deferred
response rather than being silently dropped.

The picker message becomes the single temporary progress surface: accepted,
starting, session ready/working, then at most one honest liveness update every
30 seconds. The final `/notify` replaces that same message with the existing
formatted response and session footer. Failures clean it up idempotently. The
surface uses stock Unicode emoji only; richer animation is future polish.

Native Agent status, Stop, streaming session output and customized per-wolt
sender identity are not enabled by this slice.

## Reusable private-lodge manifest

Create one Slack app per lodge from a manifest like this, then create an
app-level token with `connections:write` and install the app to the workspace.
Do not add `app_mentions:read` or channel message events for this DM-only MVP.

```yaml
display_information:
  name: Woltspace
features:
  bot_user:
    display_name: Woltspace
    always_online: false
oauth_config:
  scopes:
    bot:
      - chat:write
      - im:history
settings:
  event_subscriptions:
    bot_events:
      - message.im
  interactivity:
    is_enabled: true
  org_deploy_enabled: false
  socket_mode_enabled: true
  token_rotation_enabled: false
```

Configure the lodge only after copying the intended human's immutable Slack
member ID (starts with `U` or `W`):

```json
{
  "channels": {
    "slack": {
      "enabled": true,
      "bot_token": "<xoxb token>",
      "app_token": "<xapp token>",
      "owner_user": "<U-or-W member ID>"
    }
  }
}
```

The first live acceptance is deliberately separate: confirm the member ID with
the owner, start the lodge, verify Slack is healthy, send one owner DM, choose a
wolt, and observe the original message start the exact chosen-wolt session once.
Verify a non-owner DM and a channel mention cause no work, restart, then confirm
the same owned thread recovers. Never infer the owner from the first sender.

## Modern Slack UX and command inventory

These are follow-ups, not current behavior:

| Surface | Slack support | Additional app work |
|---|---|---|
| Slash commands | Yes | Declare each command and its request URL; Socket Mode can receive interactive payloads. |
| Global/message shortcuts | Yes | Enable Interactivity and declare callbacks. |
| Static select | Yes, via Block Kit | Implemented for owner-scoped wolt selection; Interactivity must be enabled. |
| Buttons, shortcuts and modals | Yes, via Block Kit | Future callbacks must preserve the same owner gate. |
| Suggested prompts | Yes, in Slack's agent/assistant UI | Add `assistant:write` and Agent View handlers. |
| Processing/active status | Yes | Add `assistant:write`; explicitly return status to `active`. |
| Streaming responses | Yes | Deferred; plain session replies remain the acceptance path. |
| Native Stop | Yes, through `agent_session_stopped` | Convert the app to Agent View, subscribe to the event, and map it to real session interruption. |
| Per-wolt name/icon | Yes | Add `chat:write.customize`; keep sender identity auditable and owner-controlled. |

Switching an existing Slack app to Agent View and adding scopes/events are
external app mutations and require explicit owner approval plus reinstall.

## From private setup to OAuth installation

The private MVP stores one lodge's bot/app tokens and owner ID locally. A later
multi-lodge installer should use Slack OAuth v2 for the bot installation,
persist each installation by team/enterprise ID, verify the installing user,
ask them to confirm the owner member ID, and create/rotate the Socket Mode
app-level token through an owner-controlled setup step. The reusable manifest
remains the source of truth for scopes and subscriptions. OAuth must not turn
"installer" into ambient multi-user authority: each lodge still pins one owner
and rejects every other inbound identity by default.

## Cross-channel follow-up

Telegram must adopt the same identity-bound selection invariant after this
Slack slice: when the allowed Telegram identity has no valid selected wolt,
show **Choose your wolt**; never silently fall back to `active_dog`. Keep each
channel's selection scoped to its authenticated owner identity, revalidate it
against the live eligible-wolt roster, and reopen the picker if it disappears
or becomes ineligible. This is roadmap scope only and is not implemented here.

Official references: Slack's `chat.startStream`, `assistant.threads.setStatus`,
`agent_session_stopped`, app manifests, Socket Mode and OAuth v2 documentation.
