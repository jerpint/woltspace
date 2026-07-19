# woltspace tui

Terminal cockpit for the colony. A thin client of the lodge HTTP API — vim hands, lore voice,
zero timers.

## Run

```bash
woltspace tui          # host launcher verb (installs deps on first run)
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
| `enter` | attach to the session's real tmux |
| `F12` (or `C-b d`) | detach from an attached session — back to the list |
| `o` | wake a wolt — new session (vim: *open*) |
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
- **Host or container, auto-detected** (`src/attach.js`). Host mode reaches the API at
  `localhost:7777` (override `WOLTSPACE_URL`) and attaches via
  `docker exec -it -u node <container> tmux attach`; container name auto-detected
  (override `WOLTSPACE_CONTAINER`).
- **npx-ready by construction:** plain ESM JavaScript (no JSX, no build step), runs on stock
  node >= 18, `bin` entry in package.json. Publishing it as `npx woltspace-tui` is a decision,
  not a refactor.
- Message attribution: sends identify as the host user (override `WOLTSPACE_USER`), with no
  reply-by-session line — humans aren't sessions.
