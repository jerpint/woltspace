# Woltspace 0.5.13

Python **0.5.13** is a lodge release: creating a wolt, choosing its model,
seeing and stopping sessions, and a simpler Telegram setup. The separately
distributed **@woltspace/tui remains at 0.5.2**; no npm release is required.

## Creating a wolt

- **Names as you say them.** Type "Wolter White" and the wolt is shown with
  that name in the sidebar, the Wolts page, its site and Telegram. The lodge
  works out the folder name (`wolter-white`) itself. A wolt can be renamed from
  its page; the folder name does not change. Wolts made before this release
  keep showing their folder name until you rename them.
- **Pick the model on the card.** Each working style (otter, beaver, raccoon)
  has a model dropdown. Leave it alone to get the usual model for that style.
- **A clearer create screen.** One working style is always picked (the beaver to
  start), the Create button always answers and says what is missing, and a name
  an existing wolt already uses is flagged as you type.

## Models

- **The Codex model list comes from Codex.** The lodge reads the models your
  Codex CLI offers instead of a list shipped with Woltspace, so new models show
  up on their own. The list is cached and falls back to the built-in one when
  the CLI cannot be asked.
- **Default models in Settings.** Choose which model each working style uses
  per harness. With no choice saved, Codex styles follow the CLI's own ranking.
- **Claude models are listed without version numbers**, because the names
  follow whatever your Claude Code considers current.

## Sessions

- **The Sessions tab is back**, with a count of what is online. Sessions are
  grouped by wolt and no longer jump around when the list refreshes.
- **Stop and resume in one place.** The same row, with the same stop (asks
  first) and resume control, is used on the Sessions tab, the Wolts page and
  each wolt's page.
- **A live session always opens.** If a session is still running, the terminal
  attaches to it even when it cannot be resumed.

## First run

- **A proper welcome.** A fresh lodge greets you with the starter wolt and one
  button to say hi.
- **No permission prompt on the first reply.** The starter wolt no longer
  copies files at boot, which used to stop on an approval prompt.
- `woltspace doctor` no longer reports a false keychain warning for Claude.

## Telegram

- **No extra wolt and no API key needed.** A new Telegram chat talks straight
  to your wolt: with one wolt it is that one, with several you get a list to
  pick from. `/wolt` switches later.
- **Shorter setup.** The setup skill is now: paste the bot token, send one
  message, and stay until a message has gone both ways.
- Lodges that already have a dog wolt and its API key behave as before.

## Fixes

- Saving a lodge setting no longer risks dropping other saved settings when the
  settings file cannot be read.
- A model choice that is no longer offered is ignored instead of being used.

## Upgrade notes

```sh
woltspace update
```

No colony-data migration is required. Native upgrades stop and restart the
control plane while preserving tmux sessions. Container lodges update from the
host as before.

After upgrading, verify the lodge is healthy:

```sh
woltspace status
```
