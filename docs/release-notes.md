# Woltspace 0.6.0rc3

Python **0.6.0rc3** is a release candidate. The TUI stays at
**@woltspace/tui 0.5.4**.

A plain `woltspace update` never installs a release candidate: install this
one by naming it with `--version 0.6.0rc3`. Try it on a lodge you can
restore.

## Since 0.6.0rc2

- **A lodge default of Auto needs no per-wolt grant.** With
  `"sessions": {"default_execution_policy": "auto"}` in `woltspace.json`,
  every new session on the host starts in its engine's own Auto mode, new
  wolts included, and Auto can be picked by name. Setting it is the owner's
  consent for the whole lodge. Bypass still needs the wolt's grant. Without
  the setting, a host session with no grant starts in Ask, which for Claude
  now means manual mode, even where Claude's own default is auto.

## Since 0.6.0rc1

- **A session can answer the chat its owner is talking to it from.** When
  the owner routes a Telegram or Slack chat into a session (a reply to one of
  its messages, say), `notify` to that chat is no longer refused as someone
  else's (#668).
- **A Codex session survives a slow start.** If Codex waits at its startup
  prompt longer than the lodge's discovery windows, the lodge keeps watching
  until it is ready: queued mail then reaches it, and it can rest and resume.
  While it is not ready yet, senders get one clear "queued" notice per message
  (#669).

Everything below describes 0.6 as a whole, compared with 0.5.25.

## What's new

- **Messages: an inbox for every agent, off by default.** Chat connectors,
  wolt-to-wolt messages and terminal sends can queue into a durable per-agent
  inbox, and a waker tells the agent through its engine's own input path
  instead of pasting into a live session. The owner sees waiting mail,
  read-only, on every site and on the Inbox page. Turn it on in
  `woltspace.json` with `"inbox": {"enabled": true}`; with it off every path
  behaves as before (#643).
- **Permission levels Ask, Auto and Bypass.** "Auto" now means each engine's
  own automatic mode, and the old skip-every-check behaviour is named
  "Bypass". The CLI has `--policy`, the TUI a level picker, and
  `woltspace.json` can set a lodge-wide default with
  `"sessions": {"default_execution_policy": "..."}`. Existing sessions keep
  running as they did. Ask now really asks: a new Claude session on Ask starts
  in manual mode even if Claude's own default is auto, and on the host that is
  the default for a wolt without a grant. Set the lodge default to `auto` to
  keep auto mode (#652).
- **Conversation history, opt in.** A lodge can keep a bounded archive of the
  inbox mail it admitted and the text `notify` sent, so an agent can read its
  own past conversation as context. It needs the inbox too:
  `"conversation_log": {"enabled": true}`. See
  [docs/conversation-history.md](https://github.com/jerpint/woltspace/blob/main/docs/conversation-history.md)
  (#657).
- **A Neovim client, wolts.nvim.** Pick a wolt and a session and it opens in a
  split beside your code; send a selection with a question. Alt+w switches
  between open wolts, Alt+h/j/k/l move back into the wolt, Alt+= and Alt+-
  resize. See `nvim/README.md` (#653, #656, #665).
- **Updates stay on the installed release line.** `woltspace update` installs
  a newer patch on your line; a newer line is a migration you choose with
  `--version` (#655).

## Changes to know about

- **This release changes the lodge database in a way 0.5 cannot read.**
  Starting 0.6 rewrites every stored session's permission record (0.5's
  "auto" becomes "bypass"), and from then on 0.5.x refuses to open the lodge.
  Installing 0.5.25 again does not undo it: going back means restoring the
  backup you took before upgrading.
- **Pre-releases never become the Latest release** on GitHub (#654).
- **The container's `version-check` helper is gone**; the update notice and
  `woltspace update --check` cover it (#654).
- **Sessions start from a folder whose path has a space** (#659).

## Known issues

- A session that fails right after an inbox launch can leave its launch marker
  behind, so the next launch of that agent waits for it.
- Archiving a wolt that still has queued mail leaves its mailbox in place, and
  it cannot be deleted from the lodge yet.
- There is no migration guide for 0.6 yet; these notes are the guide for this
  candidate.

## Upgrade notes

Take a backup first, with the release you are leaving, and keep it until you
are sure you are staying on 0.6:

```sh
woltspace backup --tag pre-0.6
woltspace update --version 0.6.0rc3
```

Native upgrades stop and restart the control plane while preserving tmux
sessions. A container lodge rebuilds from the host with
`woltspace rebuild --version 0.6.0rc3`.

To go back to 0.5.25, stop the lodge, reinstall 0.5.25 and start it from
the backup. `woltspace restore` unpacks into a new folder and prints the exact
`WOLTSPACE_WOLTS_DIR=... woltspace start` line to boot from it; anything that
happened on 0.6 after the backup stays in the old folder:

```sh
woltspace stop
uv tool install --force woltspace==0.5.25
woltspace restore woltspace-backup-pre-0.6.tar.gz
```

After upgrading, verify the lodge is healthy:

```sh
woltspace status
```
