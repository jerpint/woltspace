# Woltspace 0.5.21

Python **0.5.21** and **@woltspace/tui 0.5.3** are a release pair for 0.5.
Both registries publish this time.

## Thank you

- [@asharakeh](https://github.com/asharakeh) built archive, unarchive and
  delete for wolts (#575), kept large boot memory from stopping new sessions
  (#580), and fixed messages left unsent in Claude's input box (#576).

## What's new

- **Two npm doors.** `npx woltspace` opens the lodge's terminal cockpit. On a
  machine with no lodge it lists what is there and what is missing, in the
  lodge's voice, then gives the one line to paste into Claude Code, Codex or
  opencode. If woltspace is installed but stopped, it gives the start command
  instead. It never installs anything; the steps stay in the README's "For
  agents" section. `npx skills add jerpint/woltspace` brings Onboardie 🦫 into
  any agent, to explain Woltspace and install it with you, asking before each
  step. It offers that one skill only; the lodge's own skills are not listed
  (#582).
- **One Onboardie everywhere.** The skill, the starter lodge's chat skill and
  the site all use the same Onboardie and the same paste line, pointing at
  the README (#582).
- **Archive, unarchive and delete a wolt.** Put away a wolt you no longer
  need from the ⋯ menu on the Wolts page or with `woltspace wolt archive
  <name>`. It comes back exactly as it was with unarchive. Delete works only
  on an archived wolt and asks first. A held session blocks archiving, an
  archived name stays reserved, and a delete also clears that wolt's Auto
  grants and cron stamps so a later wolt with the same name starts clean
  (#575).

## Fixes

- **A wolt with a lot of memory still gets new sessions.** A new session's
  opening prompt carried the boot memory whole, and past Linux's limit for
  one argument (128 KiB) every new session exited at once while the message
  read as delivered. Oversized boot memory is now cut to fit, so the session
  starts (#580, fixes #578).
- **Messages to Claude are sent, not left in the input box.** A message with
  two or more image paths on their own lines could sit unsent while the
  lodge reported it delivered. Claude now waits half a second before Enter,
  like every other engine (#576).
- **`woltspace wolt …` works from an ordinary terminal.** The installed CLI
  had no `wolt` noun; it now hands it to the bundled client, as it does for
  `session` (#575).
- **Telegram and Slack let go of a wolt that is gone.** A chat whose wolt was
  archived or removed goes back to the wolt picker (#575).

## Upgrade notes

```sh
woltspace update
```

No colony-data migration is required. Native upgrades stop and restart the
control plane while preserving tmux sessions. Container lodges update from the
host as before.

The TUI updates on its own: `npx woltspace` or `npx @woltspace/tui` fetch
0.5.3. A global install updates with `npm install -g @woltspace/tui`.

After upgrading, verify the lodge is healthy:

```sh
woltspace status
```
