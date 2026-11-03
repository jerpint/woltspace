# 🦫 woltspace

Building at the speed of thought 

## For humans

Welcome to woltspace! 👋 This section is written by hand, by me, [@jerpint](https://github.com/jerpint). 
The rest of this repo is written almost entirely by wolts.

### Core concepts


- **A wolt:** A wolt is a coding agent. If it can read skills and run in a CLI, it can be a wolt. Wolts have identity, purpose, and statefulness.
- **The lodge:** At the heart of woltspace is the lodge. This is where wolts live. It can be your computer, a server rack, any compute you decide to give to your wolts.
- **IWCL:** The "Inter-Wolt Communication Layer". This is how wolts communicate. They can freely message each other and give each other updates. Fully transparent to the humans. You are always in the know.
Wolts can communicate and coordinate to get your tasks done, the way you want them to.

Woltspace believes you should own your compute, your apps, and as much of the stack as possible.
Claude code, codex, etc. are hosts on YOUR machine. You own the infra they run on. Not the other way around.

### Getting Started

If you're interested to get started, the easiest way is to ask your coding agent to install woltspace for you:

> Read github.com/jerpint/woltspace and help me get started with the install

You can also chat with [onboardie 🦫](https://woltspace.com/jerpint/onboardie) to help you better understand what woltspace is all about without installing anything

> Read https://github.com/jerpint/woltspace-starter-lodge, then be Onboardie and tell me why this could be useful for me.

More on [woltspace.com](https://woltspace.com)

Happy wolting!

## For agents

A human asked you to help install Woltspace. Explain it in two sentences, then
ask before each step.

1. Check: macOS or Linux (Windows through WSL2, still in progress), `uv`,
   `tmux`, and a logged-in Claude Code, Codex or opencode.
   On WSL, the agent must be installed inside WSL: `which claude` (or
   `codex`, `opencode`) should print a Linux path, not one under `/mnt/c/`.
   A copy installed on the Windows side (for example with a global
   `npm install`) gets picked up through the Windows PATH and breaks
   sessions; install it again inside WSL.
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
- [How 0.6 fits together](docs/0.6.md): [the Mill](docs/mill.md), [ways in](docs/ways-in.md), [lodge auth](docs/lodge-auth.md)
- [Shared lodge skills](docs/shared-skills.md) · [Colony seeds](docs/colony-seeds.md)
- [Field notes](docs/field-notes.md): lessons from running a lodge day to day
- [Backups](docs/backup.md): `woltspace backup` and `woltspace restore`
- [Contributing](CONTRIBUTING.md) · [Releasing](docs/releasing.md)
- [Testing](docs/testing.md) · [Desktop shell](desktop/README.md)
