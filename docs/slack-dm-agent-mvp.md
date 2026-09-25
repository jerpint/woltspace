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

The picker is replaced by a ready acknowledgement after selection. Slack's
native Agent Session becomes `processing`, receives the exact Woltspace session
slug as its title, and returns to `active` after the final reply. Each final
reply includes a validated **Open session** link to the exact spawned session
when the platform supplies its HTTPS lodge URL. Follow-ups stay pinned to the
same Slack thread and Woltspace session.

The earlier custom `Gnawing` progress-message surface is deprecated. Agent View
plus `assistant:write` is the only supported setup. Native Stop and customized
per-wolt sender identity remain future work.

## Reusable private-lodge manifest

Create one Slack app per lodge from this manifest, then create an app-level
token with `connections:write` and install the app to the workspace. Do not add
`app_mentions:read`, channel/group scopes, or channel/group message events.

```json
{
  "display_information": {
    "name": "Woltspace",
    "description": "Woltspace",
    "background_color": "#3A6644"
  },
  "features": {
    "agent_view": {
      "agent_description": "gnaw. build. repeat"
    },
    "bot_user": {
      "display_name": "Woltspace",
      "always_online": false
    }
  },
  "oauth_config": {
    "scopes": {
      "bot": ["assistant:write", "chat:write", "im:history"]
    }
  },
  "settings": {
    "event_subscriptions": {
      "bot_events": ["app_home_opened", "message.im"]
    },
    "interactivity": {"is_enabled": true},
    "org_deploy_enabled": false,
    "socket_mode_enabled": true,
    "token_rotation_enabled": false,
    "app_level_token_rotation_enabled": false,
    "is_mcp_enabled": false
  }
}
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

## Current and future Slack surfaces

| Surface | Slack support | Additional app work |
|---|---|---|
| Slash commands | Yes | Declare each command and its request URL; Socket Mode can receive interactive payloads. |
| Global/message shortcuts | Yes | Enable Interactivity and declare callbacks. |
| Static select | Yes, via Block Kit | Implemented for owner-scoped wolt selection; Interactivity must be enabled. |
| Buttons, shortcuts and modals | Yes, via Block Kit | Future callbacks must preserve the same owner gate. |
| Suggested prompts | Yes, in Slack's Agent View | Future handler and product work. |
| Processing/active status | Implemented | Agent View plus `assistant:write`; final delivery returns status to `active`. |
| Agent Session title | Implemented | Exact Woltspace slug today; human-friendly Woltspace-owned names later. |
| Streaming responses | Partially | Uses Slack's stream API when available, with a plain final-message fallback. |
| Native Stop | Yes, through `agent_session_stopped` | Convert the app to Agent View, subscribe to the event, and map it to real session interruption. |
| Per-wolt name/icon | Yes | Add `chat:write.customize`; keep sender identity auditable and owner-controlled. |

Switching an existing Slack app to Agent View and adding scopes/events are
external app mutations and require explicit owner approval plus reinstall.
Do not retain the old channel/mention manifest or custom progress-message setup
as an alternate supported mode.

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

## Session naming follow-up

The exact Woltspace session slug is the stable Agent Session title for this
MVP. The intended browsing UX is a later Woltspace-owned human-friendly session
name that is synchronized into Slack. Slack must not become the source of truth
for session identity or naming.
