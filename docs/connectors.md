# Writing a chat connector

A connector brings one chat app (Telegram, WhatsApp, Discord, Matrix, ...) to
a lodge. It is a folder: a manifest and a program. The lodge starts the
program, talks to it over a local socket, and never imports it.

Build it in your own lodge first. When it works there, the same folder can be
contributed to the woltspace repository without a rewrite.

## Where it lives

| | Folder | Who can turn it on |
|---|---|---|
| Local | `<data root>/.space/connectors/<name>/` | `channels.<name>.enabled` in the data root's config |
| Bundled (ships with woltspace) | `container/connectors/<name>/` | the same, or its `enable_env` at the platform entrypoint |

A local folder never replaces a bundled connector of the same name: pick a
new name (`telegram-mine`), or turn the bundled one off.

## The division of work

The connector translates; the lodge decides.

| The connector (your program) | The lodge |
|---|---|
| Talks to the chat app, holds its token | Runs the owner's commands (`/wolt`, `/new`, `/sessions`, ...) |
| Checks who is writing, filters out strangers | Checks the sender again against its own owner list |
| Applies the app's rules (groups, mentions, replies) | Routes messages to wolts and sessions, starts sessions |
| Downloads media into its data dir | Transcribes voice, shows images to the dog |
| Renders replies (buttons, or a numbered list) | Keeps chat state and history |
| Sends what wolts send (`notify`) | Decides where `notify` goes |

You never write command or routing logic: every connector gets the same
commands, pickers and routing.

## The manifest: `connector.json`

```json
{
  "name": "echo",
  "title": "Echo",
  "protocol": 1,
  "command": ["{python}", "echo_connector.py"],
  "capabilities": ["text", "files"],
  "command_input": ["text"],
  "events": ["message", "command", "attachment"],
  "route": [["room", "[a-z0-9]+"]],
  "secrets": [["ECHO_TOKEN", "token"]],
  "help": ["• Send a message → goes to your wolt"]
}
```

- `name`: lowercase, the folder's name.
- `protocol`: 1. An unknown version is refused.
- `command`: the program, run from the folder. `{python}` is the lodge's own
  Python; use `["uv", "run", "main.py"]` (with your own `pyproject.toml`) to
  bring your own dependencies.
- `capabilities`: what the app can do: `text`, `files`, `voice_in`,
  `voice_out`, `images`, `threads`, `edit`, `status`, `reply_to`, `reactions`.
  Files and voice notes are sent to you only if you list `files` / `voice_out`.
- `command_input`: how the owner types commands: `native` (an app menu),
  `text` (typed `/wolt`), `buttons`.
- `route`: the fields that name one conversation, each with a regular
  expression. `notify --to echo:r1` is checked against them before it reaches
  you, and sessions started from a chat reply to it the same way.
- `secrets`: each `[ENV_NAME, config_key]`. The lodge reads the value from its
  environment or from `channels.<name>.<config_key>` and hands your program
  that value only, not anything else the lodge holds.
- `help`: lines added to `/help`.

## Turning it on

In `<data root>/.space/platform/config.json`:

```json
{"channels": {"echo": {"enabled": true, "token": "...", "owners": ["your-id-in-that-app"]}}}
```

`owners` is the lodge's list of who may use the lodge through this connector:
your user id in that app. Then `woltspace restart`. `woltspace status` shows
the connector and its log is in the lodge's logs as `connector-<name>.log`.

## The program

Your program gets, in its environment:

| Variable | What |
|---|---|
| your declared secrets | e.g. `ECHO_TOKEN` |
| `WOLTSPACE_CONNECTOR_SOCKET`, `WOLTSPACE_CONNECTOR_TOKEN` | how to reach the lodge (a fresh token each launch) |
| `WOLTSPACE_CONNECTOR_DATA` | your data dir; downloads go here |
| `WOLTSPACE_CONNECTOR_OUTBOX` | the only dir files to send come from |
| `WOLTSPACE_CONNECTOR_OWNERS` | the owner ids, to filter on your side too |

`connector_client` (on the program's `PYTHONPATH`) does the socket for you:

```python
import asyncio, os
import connector_client
from connector_protocol import reply_from_wire

async def on_say(frame):           # the lodge answers in a chat
    reply = reply_from_wire(frame["reply"])
    # reply.text, reply.markdown, reply.choices (rows of Choice(label, action, url))
    await my_app.send(frame["route"]["room"], render(reply))

async def on_send(frame):          # a wolt sent something (notify)
    # frame["method"]: "message", "document", "voice" or "audio";
    # frame["path"] for a file, under WOLTSPACE_CONNECTOR_OUTBOX.
    await my_app.send(frame["route"]["room"], frame["text"])
    return {"status": "delivered"}  # or "refused", "failed", "ambiguous"

async def main():
    client = connector_client.Client("echo", on_say=on_say, on_send=on_send)
    welcome = await client.connect()          # welcome["commands"]: the menu
    async for message in my_app.messages():
        await client.event("message", {"room": message.room}, message.sender,
                           {"text": message.text}, event_id=message.id)
    # ...or keep the connection with: await client.run_forever()

asyncio.run(main())
```

Events you hand in:

| `kind` | `body` |
|---|---|
| `message` | `text`, and `group`, `addressed`, `mention` (booleans), `reply_to` |
| `command` | `name`, `args` |
| `selection` | `action` (the `action` of a choice the owner picked) |
| `attachment` | `media` (`voice`, `image`, `video`, `file`), `path` (in your data dir), `name`, `mime`, `size`, `caption` |

`reply_to` is `{"from_bot": true, "text": "...", "file_name": "...", "voice": false}`
when the owner replies to one of the bot's messages: the lodge reads the
session footer in it and routes the reply to that session.

Give each event the app's own id: if the lodge restarts before it answers,
the client sends the event again and the lodge drops the repeat.

The lodge answers each event with a receipt (`accepted`, `duplicate`,
`failed`). Then, for that event: `say` (a new message), `edit` and `ack` (for a
selection: the new body of the message the button was on, and a short toast).

`docs/connectors.md` stays the reference; the Telegram connector
(`container/connectors/telegram/`) is the complete example, and
`test/test_connector_echo.py` the smallest one that runs.

## Delivery, honestly

- Before the receipt, nothing is lost by a lodge restart: the client resends.
- After the receipt, the lodge dispatches once. If it dies in between, the
  event is lost (and a resend is a duplicate).
- Events your program holds are in memory: if your program dies, they go.

## Trust

A connector runs as the same OS user as the lodge. Getting only its declared
secrets limits what it is handed, not what it could read. Turning a local
connector on is trusting its code, like installing a package. The lodge
checks every event's sender against its own owner list, which catches a
connector's mistakes, not a connector that lies.
