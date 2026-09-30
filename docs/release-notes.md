# Woltspace 0.5.11

Python **0.5.11** gives every new lodge a first wolt to talk to. A fresh lodge
starts with **Onboardie** 🦫, a beaver who finds out what you came to build and
helps you make your first wolts. It also makes a first install on a new Mac
account work end to end. The separately distributed **@woltspace/tui remains at
0.5.2**; no npm release is required.

## A first wolt, out of the box

- **Onboardie on first run.** When a brand-new lodge finishes its first-run
  harness choice and has no wolts yet, it installs the starter lodge from
  https://github.com/jerpint/woltspace-starter-lodge, pinned to a reviewed
  commit. It happens once: deleting her never brings her back, and lodges that
  already have wolts are left alone. `WOLTSPACE_STARTER_SEED=none` turns it off;
  any other value installs a different seed. The first start needs network; if
  it's offline, the lodge reports the starter as failed and carries on.
- **"Say hi."** While the lodge's only wolt is an untouched starter, the home
  screen shows a single "Say hi" button. It opens her; she introduces herself.
- **Add her to an existing lodge** with
  `woltspace seed install https://github.com/jerpint/woltspace-starter-lodge.git`.

## Installing

- **Installers can pick the harness.** `WOLTSPACE_DEFAULT_HARNESS` (`claude`,
  `codex`, ...) set when the lodge first starts becomes the lodge default and
  skips the first-run harness question. It never overrides a choice already
  made; an invalid value keeps the question and says why. An AI agent
  installing Woltspace sets it to its own harness.
- **Works on a fresh macOS account.** Platform helpers now run on the lodge's
  own Python (`WOLTSPACE_PYTHON`). Before, they used whatever `python3` was on
  PATH, which is Python 3.9 on a new Mac account, and every session died at
  start.
- **Cleaner first start.** The data folder is created before the first start
  syncs skills and instructions, so a brand-new lodge no longer reports them as
  "not synced".

## Sessions

- **Codex sessions keep their own identity and network.** Each codex session
  now runs its own codex process instead of sharing one background daemon.
  Before, tool calls could run with another session's identity (so `notify`
  and `woltspace session send` signed as the wrong session), and a daemon
  restart could drop network access mid-session.

## Colony seeds

- **Seeds install cleaner.** A seed install gives the new wolts the platform
  skills immediately; no lodge restart is needed before their first session.
- **Public seeds.** A seed may carry a root license file (`LICENSE`,
  `LICENSE.md`, `LICENSE.txt`, `COPYING`); it's validated and never installed
  into a wolt. `woltspace seed install <git-url>@<commit>` installs exactly that
  commit and records it.
- **Skills read from where they're installed.** The start-chat skill reads its
  mode notes from beside itself instead of a container-only path, so a native
  first boot never stops on a permission prompt.

## Upgrade notes

No colony-data migration is required. Existing lodges keep their wolts and are
never given a starter. Native upgrades stop and restart the control plane while
preserving tmux sessions.

After upgrading, verify the lodge is healthy:

```sh
woltspace status
```
