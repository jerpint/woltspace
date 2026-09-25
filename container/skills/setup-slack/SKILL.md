---
name: setup-slack
description: Connect a private Woltspace lodge to its owner's Slack with the supported owner-DM Agent View app and Socket Mode flow.
---

# Slack Setup

Guide the owner through one supported setup: one owner-controlled Slack app per
lodge, Agent View, Socket Mode, and one immutable owner member ID. Work one
step at a time and stop for the owner at Slack UI boundaries.

This skill is idempotent. Inspect existing configuration without printing
tokens; preserve unrelated app settings and lodge configuration.

## Boundaries

- This connector is for direct messages from one configured owner. Do not add
  channel events, mentions, other users, or a first-sender fallback.
- Do not resurrect the legacy channel/mention bot or custom progress-message
  setup. Native Slack Agent Sessions are the supported experience.
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

## 2. Create or migrate the Slack app

Read [references/manifest.json](references/manifest.json) and give that exact
manifest to the owner as a formatted code block. Existing apps should be
updated to this manifest rather than creating a second app.

The intentional surface is:

- Agent View with the description `gnaw. build. repeat`
- bot scopes `assistant:write`, `chat:write`, and `im:history`
- bot events `app_home_opened` and `message.im`
- Interactivity and Socket Mode enabled

Do not add `app_mentions:read`, channel/group history, channel/group message
events, file scopes, `chat:write.customize`, or `agent_session_stopped`.
Those belong to separately implemented features.

After the owner saves the manifest, have them reinstall the app to the
workspace so the new bot scopes take effect.

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
presence; never echo the values. The legacy `ENABLE_SLACK_BOT` and
`SLACK_*` environment path is migration input, not the canonical setup. With
owner approval, move those values into `channels.slack`, verify the connector,
then remove the redundant legacy entries.

## 5. Activate and prove

With restart approval, record `woltspace status` and the current tmux session
count, restart through the normal Woltspace lifecycle, then verify:

- lodge health and the Slack connector are `running`;
- all pre-existing tmux sessions survived;
- the owner can DM the app, choose a wolt, and have the original message
  delivered exactly once;
- Slack shows native processing state and the Agent Session title becomes the
  exact Woltspace session slug;
- the final reply retains **Open session** and status returns to active;
- a follow-up in the same thread stays pinned to the same Woltspace session.

Do not probe with a non-owner account or post into channels unless the owner
explicitly supplies and authorizes that test. Report that those inputs fail
closed by design.

## Troubleshooting

- `not_authorized` from `agents.sessions.setStatus`: confirm Agent View,
  `assistant:write`, Save, and workspace reinstall.
- connector disabled: check all four `channels.slack` fields and that the owner
  ID begins with `U` or `W`.
- Socket Mode does not connect: confirm the app-level token has
  `connections:write` and Socket Mode is enabled.
- replies work but no native processing state: the app is still on the legacy
  surface; migrate it instead of enabling message-progress fallback.

Slack images, native Stop, custom per-wolt identity, shared OAuth installation,
and channel participation are not part of setup. Treat each as a separate
product change with its own scopes and acceptance test.

The exact session slug is the current stable title, not the final naming UX.
Woltspace should later own a human-friendly session name for browsing and sync
that name into Slack. Slack remains a presentation surface, not the source of
truth for session identity or naming.
