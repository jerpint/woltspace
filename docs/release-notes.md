# Woltspace 0.5.15

Python **0.5.15** is a 0.5 release with two new experimental engines and a few
fixes. The separately distributed **@woltspace/tui remains at 0.5.2**; no npm
release is required.

## Two new engines to try: Hermes and Pi (experimental)

- **Hermes** ([Nous Research](https://github.com/NousResearch/hermes-agent)) and
  **Pi** ([pi-coding-agent](https://www.npmjs.com/package/@earendil-works/pi-coding-agent))
  can now run a wolt, next to Claude Code, Codex and opencode. Pick one when
  you create a wolt, in Settings, or on a wolt's page.
- **They are marked experimental**, and so is opencode, until they see real
  use. The pickers say so next to their names.
- **Each engine has its own icon.** Simple line icons replace the coloured
  emoji wherever you pick an engine, and they follow light and dark mode. The
  harness choice in Create a wolt is now a menu that shows them.
- **Install them yourself, once.** Woltspace needs nothing new, but each engine
  is its own program:
  - Hermes: `uv tool install hermes-agent`
  - Pi: `npm install -g @earendil-works/pi-coding-agent` (Node 22.19 or newer)
  - Both default to Claude through OpenRouter, so put `OPENROUTER_API_KEY` in
    the lodge `.env`.

  `woltspace start` lists the engines it finds and says whether Hermes has its
  key.
- **Each Hermes wolt keeps its own Hermes home** (`<wolt>/.hermes`), with the
  wolt's skills, its own persona file and no Hermes memory of its own (a wolt's
  memory stays in `wolt/memory/`). No credentials are copied into it.
- **Pi runs without project trust**, so a repository's own Pi extensions never
  run. A Pi wolt gets its skills through `--skill`.
- Known limits are listed in
  [adding a harness](adding-a-harness.md#experimental-harnesses-known-limits):
  neither engine is in the container image, and both are untested from
  Telegram and Slack.

## Fixes

- **A voice note is never lost silently.** When Telegram was slow to hand over
  the audio, the download timed out and the message vanished with no reply.
  Downloads now retry with longer timeouts. If they still fail, the bot answers
  "I couldn't get that voice message from Telegram (it kept timing out). Please
  send it again." Photos, videos and files get the same retries.
- **Every wolt shows its own creature again.** On some page loads, the
  sessions list was drawn before the lodge knew which creature each wolt is,
  so every group showed the beaver, and the folder name instead of the wolt's
  display name, until a full reload. The sidebar now corrects itself as soon
  as the wolts load, and on every refresh after that. It happened most on
  fresh lodges, slower machines and lodges opened remotely. Thanks to the
  community member who traced it to the exact lines.
- **Wolts know about Onboardie.** A wolt's instructions now say that Onboardie,
  the starter lodge's beaver, gives newcomers the tour and helps with their
  first wolt, and that a wolt should check the local `wolts/*/wolt/wolt.json`
  files before asking who another wolt is.
- **The startup check no longer fails on an engine it does not know.**

## Docs

- **The README has two parts: one for humans, one for agents.** An agent can
  follow the install steps on its own.

## Also in this release

0.5.15 includes everything that shipped in 0.5.14: fresh lodges install the
newest Onboardie, and 0.5 fixes can ship from `release/0.5`.
