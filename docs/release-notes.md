# Woltspace 0.6.0rc5

Python **0.6.0rc5** is a release candidate, with **@woltspace/tui 0.6.0**.

A plain `woltspace update` never installs a release candidate: install this
one by naming it with `--version 0.6.0rc5`. Try it on a lodge you can
restore.

## Since 0.6.0rc4

- **The TUI moves to 0.6.0.** A 0.5 TUI on a 0.6 lodge kept warning that the
  lodge was newer and to install `@woltspace/tui@latest`, which was that same
  0.5 TUI. The code is unchanged; only the version moves, so the warning
  stops. It still works with 0.5 lodges.
- **Keep every session: an opt-in session archive.** Engines delete their own
  chats (Claude Code after 30 days by default). With
  `"sessions": {"archive": true}` in `woltspace.json`, the lodge keeps a copy
  of every engine session file in `.space/session-archive/`, can send it
  encrypted to its backup bucket, list it, restore it byte for byte, and
  delete one session on purpose. Woltspace never writes to any engine's
  settings. Unset, nothing changes (#680).
- **WhatsApp, opt-in.** The Connectors page can set up WhatsApp through the
  Agent Platform: the key is entered in a private terminal, and the
  connector serves one configured creator, with the same commands, pickers
  and inbox as the other chat connectors (#683).
- **Antigravity, experimental.** A new engine runs Google's Antigravity
  (`agy`) in its own terminal, using the machine's Antigravity login. It
  starts in Ask, resumes its exact conversation, and is off unless you pick
  it (#682).
- **Codex keeps its permission level.** A native Codex session now passes
  Ask, Auto or Bypass to its app-server on start and on resume, and refuses
  to start on a permission setting it doesn't recognise. This fixes the
  rc2 known limitation for Codex with native inbox hints (#676).
- **Stopping an app only stops that app.** After a restart, an app's saved
  process number can belong to another program. The lodge now checks a
  process really is the app (its start time, or for older records its
  folder, on macOS too) before stopping or keeping it; if it can't tell, it
  touches nothing and says so (#687).
- **No Inbox tab in the lodge sidebar.** Waiting mail still shows in the
  pill on each wolt's site, and the page still opens at `/inbox` (#688).
- **Resume with an empty body works.** `POST /sessions/{name}/resume` with an
  empty, invalid or list body now resumes with no prompt instead of an error
  (#681).
- **Turning the inbox on: restart running sessions.** A session gets the
  inbox's hooks when it starts, so sessions already running when you turn
  the inbox on are not woken by it. Restart them; every session started
  after works.

## Since 0.6.0rc3

- **A wolt sees what its other sessions are doing.** A wolt often runs
  several sessions at once: one per chat, a spawned helper, a scheduled run.
  A new session now starts with a short board of its wolt's other live and
  recent sessions: status, last activity, where each listens, and the title
  and line each wrote with `woltspace session describe`. `woltspace session
  board` prints it at any time. A wolt only ever sees its own sessions, the
  board is left out when boot memory needs the room, and a session that never
  described itself is only counted (#677).
- **The lodge reports its real permission default.** `/runtime/capabilities`
  answered Ask on the host even when the lodge default was Auto, so a client
  that copies that value into its request (older TUI versions did) started
  sessions in Ask. It now answers with the same default a new session gets.
  Each session start also logs the level that was asked for and the level
  used, and warns when the lodge default is one the engine does not offer
  (#678).
- **Slack is a chat connector, like Telegram.** One DM with an active wolt:
  `/wolt` and `/new` switch, a reply under a wolt's message goes to that
  wolt, and voice notes, images, files, the dog and the inbox work as on
  Telegram. See "Changes to know about" before upgrading a lodge that uses
  Slack (#670).
- **Your first message waits while you pick a wolt.** On every chat
  connector, a message sent with no wolt chosen is held for up to 10 minutes
  and delivered once to the wolt you pick, instead of "pick a wolt, then send
  it again" (#671).

## Since 0.6.0rc2

- **A lodge default of Auto needs no per-wolt grant.** With
  `"sessions": {"default_execution_policy": "auto"}` in `woltspace.json`,
  every new session on the host starts in its engine's own Auto mode, new
  wolts included, and Auto can be picked by name. Setting it is the owner's
  consent for the whole lodge. Bypass still needs the wolt's grant. Without
  the setting, a host session with no grant starts in Ask, which for Claude
  now means manual mode, even where Claude's own default is auto.
- **Known limitation: Codex Auto and Ask with native inbox hints.** With
  `"inbox": {"native_hints": true}`, a Codex session runs against its own
  app-server, which carries only Bypass: Auto and Ask are not passed to it, and
  a resumed Auto session keeps a flag a remote resume may refuse. Keep native
  hints off for Codex wolts on Auto or Ask until this is fixed before 0.6.0.

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
- **Slack changes shape, and its app needs one reinstall.** Slack is now one
  flat DM instead of one thread per session, and the "Agent View" status is
  gone. Slack keeps `/apps` for itself, so the apps list is `/wapps`; inside a
  thread type `!wolt`, `!new` or `!help`. The `channels.slack` settings are
  unchanged, but the Slack app gains the `commands` and `files:read` scopes
  and drops `assistant:write`: update its scopes and reinstall it once (#670).
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
woltspace update --version 0.6.0rc5
```

Native upgrades stop and restart the control plane while preserving tmux
sessions. A container lodge rebuilds from the host with
`woltspace rebuild --version 0.6.0rc5`.

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
