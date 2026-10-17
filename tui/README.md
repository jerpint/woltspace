# woltspace tui

Terminal cockpit for the colony. A thin client of the lodge HTTP API — vim hands, lore voice,
zero timers.

## Run

```bash
woltspace start
woltspace tui          # the local TUI if one is installed, else npx @woltspace/tui@latest
```

or directly:

```bash
cd tui && bun install
node src/main.js       # node >= 18 or bun, host or in-container
```

## The list

Sessions are drawn as a tree, like the lodge's sidebar: each wolt is a folder
with its running count (`softie 2/4`), its sessions are leaves showing their own
title (or their name, dimmed, when they have none). `●` running, `📌` held,
`○` resting. Wolts with nothing running fold into one "resting" row at the
bottom. A summary line counts running, resting and held sessions. On a terminal
of 100 columns or more, a pane on the right shows the selected row: title,
summary, state, when it rests if unused (or why it is held), engine, where it
came from, last use, policy and folder.

## Keys

The keys are always shown at the bottom of the list.

| key | action |
| --- | --- |
| `j/k` `gg/G` `ctrl-d/u` | move / jump top-bottom / half-page |
| `h/l` (or `←/→`) | fold / unfold a wolt or the resting row; `h` on a session folds its wolt |
| `o` or `enter` | on a session: attach to its real tmux (a resting one is woken first). On a wolt or the resting row: fold or unfold. Inside your own tmux the wolt renders in the pane you are in - the session stays where it is, your prefix and pane keys keep working |
| — | there is no key to leave a session, because tmux already has one for each case. Quit claude to end it and fall back to the list; `prefix c` to leave it running and work elsewhere; `prefix &` to take the pane back, which kills the viewer and never the wolt |
| `n` | new session: a screen to pick a wolt (starting on the one you are on), then confirm |
| `c` | new wolt: name, creature, confirm cwd/policy, and start its first session |
| `s` | send an attributed message into the selected session |
| `x` | stop the selected session (`y/N` confirm) |
| `r` | refetch |
| `/` `tab/shift-tab` | search (it unfolds everything) / next / prev match |
| `a` | toggle stopped sessions |
| `q` | quit |

`n` and `c` take over the list with one card; `esc` steps back, and from the
first step returns to the list.

New sessions start in the wolt's home and use the server's permission default.
On a native lodge, an existing Auto grant for that exact wolt and directory
selects Auto; without a matching grant, the session uses Prompt. The confirmation
shows "server default" because the lodge resolves the grant when it spawns.

## Design

- **Request/response only, no polling.** Fetches on launch, after every action, and on `r`.
  The header says *as of HH:MM:SS* — honest, not fake-live. When the event feed ships it plugs
  in behind `src/api.js` as a push transport.
- **One attach primitive:** runtime capabilities select direct inherited-stdio tmux for a
  native lodge or an in-container TUI. A host TUI talking to the external/container lodge
  retains the Docker exec compatibility path (`WOLTSPACE_CONTAINER` overrides its name).
- **Independent releases:** the tui declares the minimum woltspace version it needs
  (`minLodgeVersion` in `src/version.js`) and checks it against the lodge's `/health`;
  woltspace does not check the tui's version. Its launcher accepts a local binary when
  `--version --json` reports this package and this bin, whatever the version, and otherwise
  falls back to `npx @woltspace/tui@latest`. Install or upgrade each half on its own.
- **npx-ready by construction:** plain ESM JavaScript (no JSX or build step), stock Node.js
  18+, scoped package metadata, and a single `woltspace-tui` bin entry.
- Message attribution: sends identify as the host user (override `WOLTSPACE_USER`), with no
  reply-by-session line — humans aren't sessions.
