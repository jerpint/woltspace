# wolts.nvim

Talk to your woltspace wolts from Neovim, like a Claude / Codex side panel.

- Pick any wolt in any of your lodges, then a live session, a resting one (woken for you) or a new one.
- The session opens in a terminal split on the right: it is the real session (`tmux attach`), the same one `woltspace tui` shows.
- Ask about code: select lines, press a key, type a question. The wolt gets it with a `@file#L12-20` reference.
- Zoom the wolt to full screen and back.

## Needs

- Neovim 0.10 or newer
- `curl` and `tmux`
- `docker`, only for a lodge that runs in a container
- A running woltspace lodge you can reach over HTTP (by default `http://127.0.0.1:7777`)

## Install

With [lazy.nvim](https://github.com/folke/lazy.nvim):

```lua
{
  'woltspace/wolts.nvim', -- placeholder: the repo it ships in
  config = function() require('wolts').setup() end,
}
```

With the built-in `vim.pack` (Neovim 0.12+):

```lua
vim.pack.add { 'https://github.com/woltspace/wolts.nvim' } -- placeholder URL
require('wolts').setup()
```

Or by hand: put this folder on your `runtimepath` and call `require('wolts').setup()`.

## Keys

| Key | What it does |
| --- | --- |
| `<leader>Wp` | Pick a wolt, then a session (or `+ new session`). It opens on the right. |
| `<leader>Ww` | Show / hide the current wolt's split |
| `<leader>Wa` | Ask the current wolt about this line. In visual mode: about the selection. A box opens; type, then `<C-s>` (or `<CR>` in normal mode) sends. `q` / `<Esc>` cancels. |
| `<leader>Wz` · `<M-z>` | Zoom: the wolt alone, full screen; again to go back. `<M-z>` also works while you type in the wolt. |
| `<M-h/j/k/l>` | In the wolt's terminal: leave it and move to the next window |

`:Wolts pick | toggle | ask | zoom` does the same without keys (`:'<,'>Wolts ask` for a range).

Messages you send are marked as coming from you: `$WOLTSPACE_USER`, else `$HUMAN_NAME`, else `$USER` (the same rule as `woltspace tui`).

## Setup

All options, with their defaults:

```lua
require('wolts').setup {
  lodges = {
    { name = 'home', url = 'http://127.0.0.1:7777' },
  },
  user = nil, -- default: $WOLTSPACE_USER, $HUMAN_NAME, $USER, then 'human'
  resting_shown = 8, -- resting sessions offered per wolt, newest first
  width = 0.45, -- the wolt split, as a share of the screen
  keys = true, -- false: set your own keys (see below)
}
```

### More than one lodge

`lodges` is a list; the picker shows every wolt of every lodge, labelled with the lodge name. Each lodge takes:

| Field | Meaning |
| --- | --- |
| `name` | Short label shown in the picker |
| `url` | The lodge's HTTP API |
| `container` | The lodge runs in docker: attach with `docker exec`. `true` finds the container the way `woltspace tui` does (the first running one with `woltspace` in its name), or give the container's name. |
| `container_user` | The user inside the container (default `node`) |
| `from` | Sender name on messages to this lodge (default: `user`) |
| `relative_paths` | File references relative to the git root (else `~/...`), never absolute paths |

For example, a lodge on your machine and a second one in a container whose wolts publish their work:

```lua
require('wolts').setup {
  lodges = {
    { name = 'home', url = 'http://127.0.0.1:7777' },
    { name = 'box', url = 'http://127.0.0.1:7778', container = true, from = 'human', relative_paths = true },
  },
}
```

`from = 'human'` and `relative_paths = true` keep your name and your home path out of what those wolts see, so neither can end up in their commits or pull requests.

### Your own keys

```lua
local wolts = require 'wolts'
wolts.setup { keys = false }
vim.keymap.set('n', '<leader>aa', wolts.pick)
vim.keymap.set('n', '<leader>at', wolts.toggle)
vim.keymap.set('n', '<leader>az', wolts.zoom)
vim.keymap.set('n', '<leader>ak', function() local l = vim.fn.line '.'; wolts.ask(l, l) end)
```

## How it works

- Wolts and sessions come from the lodge API: `GET /wolts`, `GET /sessions`.
- A resting session is woken with `POST /sessions/<name>/resume`; a new one is started with `POST /sessions/new/lodge`.
- The split runs `tmux attach -t <session>` (or `docker exec -it ... tmux attach` for a container lodge) in a Neovim terminal. Closing the split leaves the session running.
- A question goes to `POST /sessions/<name>/message`, with the sender name from `user` (or the lodge's `from`).
- If you use [persistence.nvim](https://github.com/folke/persistence.nvim), wolt splits are hidden before a layout is saved (a saved layout can't bring a terminal back).

## Troubleshooting

- **`no lodge answered`**: the lodge isn't reachable at `url`. Check with `curl http://127.0.0.1:7777/wolts`.
- **`attach exited 1`**: tmux couldn't find the session. For a container lodge, check `docker ps` shows it and that `container_user` is right.
- **Esc doesn't reach Claude / Codex**: if your config maps `<Esc><Esc>` to leave terminal mode, the plugin keeps `<Esc><Esc>` for the wolt in its own split. Use `<C-\><C-n>` or `<M-h/j/k/l>` to leave it.

## Author and license

By Ali Harakeh ([@asharakeh](https://github.com/asharakeh)). MIT license, see [LICENSE](LICENSE).
