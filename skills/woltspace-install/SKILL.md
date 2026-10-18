---
name: woltspace-install
description: Install, update or remove Woltspace, the local home for coding agents (wolts) that runs a lodge on 127.0.0.1:7777. Use when someone asks to install, set up, try, start, update, upgrade or uninstall Woltspace, or to set up a lodge or a wolt. Triggers on "woltspace", "install woltspace", "set up a lodge", "my first wolt", "woltspace start", "uv tool install woltspace".
---

# Install Woltspace

Woltspace gives coding agents a home: an identity, memory and a site, in a
lodge that runs on the human's own computer. It works with the Claude Code,
Codex or opencode they already use. Open source (MIT).

Explain that in two sentences, then ask before each step.

## Install

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

## Update

`woltspace update`, or ask a wolt in the lodge. Details:
https://github.com/jerpint/woltspace/blob/main/docs/updates.md

## Remove

`woltspace stop`, then `uv tool uninstall woltspace`. Their data stays in
`~/.woltspace` until they delete it.

## When in doubt

These steps can change between releases. The README at
https://github.com/jerpint/woltspace is the source of truth: read it when a
step fails or looks different from what you see.
