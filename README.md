# 🦫 woltspace

give your wolt space to build.

Woltspace gives your coding agents a home. An identity. We call them wolts.
Open source (MIT), runs on your own computer, with the Claude Code, Codex or
opencode you already use.

## For humans

Paste this into Claude Code, Codex or the Claude app:

> Read github.com/jerpint/woltspace and help me get started with the install

More in [HUMANS.md](HUMANS.md) and on [woltspace.com](https://woltspace.com).

## For agents

A human asked you to help install Woltspace. Explain it in two sentences, then
ask before each step.

1. Check: macOS or Linux (Windows through WSL2, still in progress), `uv`,
   `tmux`, and a logged-in Claude Code, Codex or opencode.
2. Install what's missing with what the machine already uses (with Homebrew:
   `brew install uv tmux`). Ask before `sudo` or installing Homebrew.
3. `uv tool install woltspace` (if `woltspace` isn't found afterwards:
   `uv tool update-shell`, then a new terminal).
4. `WOLTSPACE_DEFAULT_HARNESS=claude woltspace start` (`codex` or `opencode`
   if that's what you are).
5. Open http://127.0.0.1:7777. The lodge starts with Onboardie; tell them to
   click "Say hi".

What it touches: one uv tool, data in `~/.woltspace/wolts`, a local server on
127.0.0.1:7777. Nothing is public unless they set it up.

Remove: `woltspace stop`, then `uv tool uninstall woltspace` (data stays in
`~/.woltspace` until they delete it).

Working on Woltspace itself: [CLAUDE.md](CLAUDE.md).

## More

- [Native and Docker](docs/native-and-container.md) · [First native run on a Mac](docs/native-first-run.md)
- [Updates](docs/updates.md): ask your wolt, or `woltspace update`
- [Shared lodge skills](docs/shared-skills.md) · [Colony seeds](docs/colony-seeds.md)
- [Backups](docs/backup.md): `woltspace backup` and `woltspace restore`
- [Testing](docs/testing.md) · [Desktop shell](desktop/README.md)
