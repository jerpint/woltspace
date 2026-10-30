# Woltspace 0.5.25

Python **0.5.25** and **@woltspace/tui 0.5.4** are published together.

## What's new

- **The TUI shows custom creatures.** A wolt that is a custom creature shows
  its own emoji in the TUI's session list and in "wake a wolt" (#633, #646).
- **The TUI runs on Ink 8 and React 19.** Ink 5 counted one-character emoji
  such as ⭐ as one column, so a row with one wrapped and left a blank line.
  Ink 8 measures them as terminals draw them (#646).

## Changes to know about

- **The TUI needs Node.js 22 or newer** (was 18). `woltspace tui` says so
  when the Node it finds is older. CI, the release workflow and the container
  image already use Node 22 or newer (#646).
- **The unused v1 Telegram adapter is gone.** The Telegram connector from
  0.5.24 is the only Telegram path (#636).

## Upgrade notes

Take a backup first, with the release you are leaving:

```sh
woltspace backup --tag pre-upgrade
woltspace update
```

Native upgrades stop and restart the control plane while preserving tmux
sessions. Container lodges update from the host as before.

If you run the TUI with `npx woltspace` or `woltspace tui`, update Node.js to
22 or newer first.

Going back to 0.5.24 works without extra steps: nothing in the lodge's data
changed.

After upgrading, verify the lodge is healthy:

```sh
woltspace status
```
