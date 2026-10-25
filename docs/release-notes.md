# Woltspace 0.5.22

Python **0.5.22** is published by itself. **@woltspace/tui** stays at
**0.5.3**: nothing in the TUI changed.

## Thank you

- [@asharakeh](https://github.com/asharakeh) added the options for running a
  second, locked-down container lodge on one host, and hardened the
  container's root phase for everyone (#604).

## What's new

- **Voice notes go to any transcription service.** Telegram voice notes are
  transcribed by the service set in `woltspace.json` `transcription`: any
  OpenAI-style `/audio/transcriptions` endpoint, its model and the variable
  in `.env` that holds its key. Set it with `woltspace transcription
  show|set|clear`. A model on this machine works over `http://127.0.0.1`
  with `--no-key`. The key never goes into the file, and a key is never sent
  to a new server unless you name it (#567).
- **Spoken messages from any speech service.** `notify --speak` uses
  `woltspace.json` `speech`: any OpenAI-style `/audio/speech` endpoint, its
  model, voice and key variable, set with `woltspace speech show|set|clear`.
  It's the same rules as transcription, including a local model with
  `--no-key`. `OPENAI_TTS_MODEL` and `OPENAI_TTS_VOICE` still apply when
  `speech` leaves them out (#605).
- **A second, locked-down container lodge.** New opt-in launcher and build
  options, all off by default: `WOLTSPACE_NODE_SUDO=false` (no sudo, and the
  container runs with `no-new-privileges`), `WOLTSPACE_CLAUDE_MANAGED_SETTINGS`,
  `WOLTSPACE_PUBLISH_HOST`, `WOLTSPACE_DOCKER_NETWORK` and `WOLTSPACE_ENV_FILE`.
  `woltspace build` builds the image without stopping the running lodge,
  so the downtime is only the restart (#604).
- **A contributing guide.** `CONTRIBUTING.md` covers how changes get in,
  branches and release lines, and the conventions for contributing from a
  lodge (#602). Every path now has a code owner (#600).

## Changes to know about

- **The default transcription model is now `gpt-4o-transcribe`** (it was
  `whisper-1`). With no `transcription` section, voice notes still go to
  OpenAI with `OPENAI_API_KEY`, using the newer model. To keep the old one:
  `woltspace transcription set --model whisper-1` (#567).
- **The container's root phase calls system tools by absolute path**, so a
  file a session leaves on `PATH` can't run as root at the next start. This
  applies to every container lodge (#604).

## Upgrade notes

```sh
woltspace update
```

No colony-data migration is required. Native upgrades stop and restart the
control plane while preserving tmux sessions. Container lodges update from the
host as before.

The TUI is unchanged at 0.5.3.

After upgrading, verify the lodge is healthy:

```sh
woltspace status
```
