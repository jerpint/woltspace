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

## Keys

| key | action |
| --- | --- |
| `j/k` `gg/G` `ctrl-d/u` | move / jump top-bottom / half-page |
| `enter` | attach to the session's real tmux. Inside your own tmux the wolt renders in the pane you are in - the session stays where it is, your prefix and pane keys keep working |
| — | there is no key to leave a session, because tmux already has one for each case. Quit claude to end it and fall back to the list; `prefix c` to leave it running and work elsewhere; `prefix &` to take the pane back, which kills the viewer and never the wolt |
| `n` | start a session for an existing wolt |
| `c` | create a wolt, confirm cwd/policy, and start its first session |
| `s` | send an attributed message into the selected session |
| `x` | stop the selected session (`y/N` confirm) |
| `r` | refetch |
| `/` `n/N` | search / next / prev match |
| `a` | toggle dead sessions |
| `q` | quit |

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
