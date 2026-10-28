# Woltspace 0.5.24

Python **0.5.24** is published by itself. **@woltspace/tui** stays at
**0.5.3**. The TUI's custom-creature emoji (#633) ships with the next TUI
release; until then the TUI shows a custom creature with the default glyph.

## Thank you

- [@asharakeh](https://github.com/asharakeh) brought custom creatures onto
  current main (#633), gave each wolt its own speaking voice (#625), put the
  container entrypoint under tini (#627), corrected the backup summary and
  speech timeouts (#634), and documented container time zones (#624).

## What's new

- **Telegram is a connector.** It ships as a folder
  (`container/connectors/telegram/`) and runs as its own process. It talks to
  the lodge over a local socket and gets only its own secrets. The lodge now
  owns the commands, buttons, message routing, the dog and chat state; the
  connector translates and renders. For a Telegram user nothing should look
  different. A new chat app can be tried from
  `<data root>/.space/connectors/<name>/` without a fork; `docs/connectors.md`
  is the guide (#620).
- **`notify --to CONNECTOR:ROUTE`** sends to any connector by its declared
  route fields (#620).
- **Custom creatures.** A new name and emoji on one of the three sizes
  (otter, beaver, raccoon), with its own model per engine and a default role:
  `woltspace creature add|list|rm|assign`, or ask a wolt (the creatures
  skill). The emoji shows in the lodge, the site shell, Telegram and Slack.
  See `docs/custom-creatures.md` (#633).
- **A voice per wolt.** A wolt can set `"speech": {"voice": ...,
  "instructions": ...}` in its `wolt.json`, so wolts sound different on
  `notify --speak`. Without it nothing changes (#625).
- **Slim backups by default.** All context, code and text files up to 5 MB go
  in; media and large files are left out and listed in
  `backup-left-out.tsv`. `woltspace backup --full` keeps the old behaviour. A
  restored copy is disarmed so it never touches its source (#585).
- **Container lodges run under tini**, which collects orphaned processes, so
  zombies no longer pile up and `woltspace stop` no longer waits on a
  control plane that has already exited (#627).

## Changes to know about

- **Shared settings writes are serialized.** Writers of `woltspace.json` and
  each `wolt.json` hold a lock from read to write, so two changes at the same
  moment no longer overwrite each other (#633).
- **A Telegram event is handled at most once.** The lodge records each
  event's id before acknowledging it, so a resend is dropped. If the lodge
  dies between the acknowledgement and handling, that event is lost (#620).
- **The container's per-wolt Telegram adapter override is gone.** A lodge
  that put its own `wolt/bot/telegram_adapter.py` in a wolt should move it to
  a connector folder (#620).
- **The Telegram connector's log no longer records request URLs**, which
  carried the bot token (#620).
- **Speech requests wait up to 50 s for a response to start** (was 30 s), and
  backup summaries count included and left-out credential files correctly
  (#634).

## Upgrade notes

Take a backup first, with the release you are leaving:

```sh
woltspace backup --tag pre-upgrade
woltspace update
```

Native upgrades stop and restart the control plane while preserving tmux
sessions; the Telegram connector starts with it. Container lodges update from
the host as before and get tini on their next image build.

Going back to 0.5.23 works without extra steps: the one new `lodge.db` table
is additive.

After upgrading, verify the lodge is healthy:

```sh
woltspace status
```
