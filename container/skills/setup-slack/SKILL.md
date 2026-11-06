---
name: setup-slack
description: Connect a private Woltspace lodge to its owner's Slack: one owner-only DM app over Socket Mode, run as the lodge's Slack connector.
---

# Slack Setup

Guide the owner through one supported setup: one owner-controlled Slack app per
lodge, Socket Mode, and one immutable owner member ID. Slack then works like
Telegram: one direct message with the app, an active wolt, the lodge's commands,
the dog, files and voice. Work one step at a time and stop for the owner at Slack
UI boundaries.

This skill is idempotent. Inspect existing configuration without printing
tokens; preserve unrelated app settings and lodge configuration.

## Boundaries

- The connector takes direct messages from one configured owner only. Do not add
  channel events, mentions, other users, or a first-sender fallback.
- Never ask the owner to paste `xoxb-` or `xapp-` secrets into Slack. Have them
  enter secrets on their lodge machine, or use an already-approved private
  configuration surface.
- Creating or changing the Slack app, reinstalling it, writing secrets, and
  restarting the lodge are separate external/local mutations. Explain the
  next mutation and obtain approval when the current request did not already
  authorize it.
- Never expose token values in commands, logs, validation output, or summaries.

## 1. Inspect without secrets

Find the active Woltspace data root and its
`.space/platform/config.json`. Report only whether `channels.slack` exists,
whether it is enabled, and which required fields are present. Check
`woltspace status` for the Slack connector. Do not print the configuration
object or environment.

If Slack is already healthy, ask the owner to send a DM and validate the
acceptance flow instead of rebuilding the app.

## 2. Create or update the Slack app

Read [references/manifest.json](references/manifest.json) and give that exact
manifest to the owner as a formatted code block. An existing app is updated to
this manifest rather than replaced by a second app.

The intentional surface is:

- bot scopes `chat:write`, `commands`, `files:read`, `files:write` and `im:history`;
- the bot event `message.im`, the Messages tab, Interactivity and Socket Mode;
- the slash commands `/wolt`, `/new`, `/sessions`, `/holds`, `/idle`, `/apps`,
  `/kill` and `/help`.

`files:read` lets the connector download what the owner shares; `files:write`
lets a wolt send a file. An app from before this connector carried Agent View
and `assistant:write`: drop both. Do not add `app_mentions:read`,
channel/group history or channel/group message events.

After the owner saves the manifest, have them reinstall the app to the
workspace once so the new scopes and commands take effect.

## 3. Create the Socket Mode token

In the Slack app's **Basic Information → App-Level Tokens**, create a token
with scope `connections:write`. This produces the private `xapp-` token.
The workspace installation produces the private `xoxb-` bot token.

Have the owner copy their immutable Slack member ID from **Profile → More →
Copy member ID**. It must begin with `U` or `W`; never infer it from the first
message.

## 4. Configure the lodge

Merge this object into `.space/platform/config.json`, preserving every
unrelated key:

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

Keep the file private. Validate only token prefixes, owner-ID shape, and field
presence; never echo the values. The legacy `ENABLE_SLACK_BOT` and `SLACK_*`
environment path still works and is migration input, not the canonical setup.
`SLACK_NOTIFY_CHANNEL`, when set, is where a notification goes that names no
chat (a wolf ping to Slack).

## 5. Activate and prove

With restart approval, record `woltspace status`, restart through the normal
Woltspace lifecycle, then verify:

- lodge health and the Slack connector are `running`;
- a DM reaches the active wolt (with none chosen: the picker, and the first
  message is delivered once after the pick);
- `/wolt`, `/help` and `!help` (typed, also inside a thread) answer;
- a reply in a thread under a wolt's message reaches that wolt;
- a file and a voice note reach the wolt, and `notify --file` arrives in the DM.

Do not probe with a non-owner account or post into channels unless the owner
explicitly supplies and authorizes that test. Report that those inputs fail
closed by design.

## Troubleshooting

- connector disabled: check all four `channels.slack` fields.
- every message ignored: `owner_user` is not the sender's member ID.
- Socket Mode does not connect: confirm the app-level token has
  `connections:write` and Socket Mode is enabled.
- a slash command answers "dispatch_failed" or nothing: the app was not
  reinstalled after the commands were added. Typed `!` commands work meanwhile.
- a file send fails naming `files:write`, or shared files never arrive
  (`files:read`): update the manifest, Save, and reinstall.
