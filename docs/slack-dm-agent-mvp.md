# Slack as a connector

Slack is a folder connector the lodge ships, `container/connectors/slack/`
(`connector.json` and `slack_connector.py`), like Telegram: its own process,
talking to the lodge over its socket (`docs/connectors.md`). It only translates.
Routing, commands, the dog, the inbox and history are the lodge's shared chat
code (`chat_lodge`, `lodge_messages`, `lodge_commands`), so Slack works as
Telegram does.

## What the owner sees

- **One flat DM.** Messages go to the chat's active wolt (`/wolt` switches,
  `/new` starts fresh). With no wolt chosen and no dog, the picker; the first
  message is held and goes to the wolt picked.
- **Commands.** `/wolt /new /sessions /holds /idle /kill /help` as Slack
  slash commands at the top level of the DM. Slack runs no custom slash command
  inside a thread, so every command can also be typed with `!` anywhere
  (`!wolt alpha`, `!help`); `!!` sends the text as it is. Buttons cover the pickers.
- **Replies.** A reply in a thread under one of this bot's messages goes to that
  message's session, as a reply on Telegram does. Anything else in a thread is a
  plain message.
- **Files and voice.** Shared files are downloaded (`files:read`) into
  `.space/slack/uploads/` and passed on; audio is transcribed.
- **Notifications.** `notify` text and `--file` arrive in the DM.

## What the connector checks

Only `message.im` events (and the `file_share` subtype) from the one configured
member ID, in a `D…` channel, each Slack event once. Slash commands and clicks
from anyone else, or from outside the DM, are dropped. A command or a click is
acknowledged before anything else, inside Slack's 3 seconds. The lodge checks the
owner again.

## Configuration

The same block as before, in the data root's `.space/platform/config.json`:

```json
{"channels": {"slack": {"enabled": true, "bot_token": "<xoxb>", "app_token": "<xapp>",
                        "owner_user": "<U or W member ID>"}}}
```

`ENABLE_SLACK_BOT`, `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` and `SLACK_OWNER_USER`
still work at the platform entrypoint. `SLACK_NOTIFY_CHANNEL` is the route a
notification takes when it names none (a wolf ping), and stays one of the
lodge's own chats.

The Slack app: bot scopes `chat:write`, `commands`, `files:read`, `files:write`,
`im:history`; app-level token `connections:write`; event `message.im`;
Interactivity, Socket Mode and the Messages tab on; seven slash commands
(`/apps` is a Slack built-in, so it is typed as `!apps`).
The manifest is `container/skills/setup-slack/references/manifest.json`, and the
`setup-slack` skill walks an owner through it. Agent View and `assistant:write`
are no longer used.

## Old routes

Routes from before the connector are read as the flat DM: `notify --slack C T`
and `--to slack:C:T` ignore the thread, and a session's stored
`{"adapter": "slack", "chat_id": C, "thread_ts": T}` routes to `C`. Only flat
routes are written now. Old thread-to-session rows stay in the lodge database as
history; a reply in an old thread routes like any message.

## Release

A 0.6 change: thread-per-session routing and Agent View are replaced. The app
needs its new manifest and one reinstall.
