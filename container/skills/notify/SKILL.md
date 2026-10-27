---
name: notify
description: Push a message, a file or a voice note back to the user on Telegram or Slack — from inside a session.
---

# Notify — Push Messages to the User

WhatsApp is available through the official Agent Platform for text-only
messages. Use `notify --whatsapp 'user:<digits>'` with the exact participant
identifier from a recent inbound message. Files and audio are unsupported.

Sessions can send messages back to the user at any time. Use this when you finish something worth reporting, hit a blocker that needs input, or find something the user should know about mid-task.

## Session owner notification tool

If this session exposes `notify_owner`, prefer that tool for WhatsApp text
updates. Supply only `message`, without the creature/name prefix; the lodge
adds the identity and fixes the owner destination. It needs no routine shell
approval. Do not provide a session ID, participant, URL, channel, file or path.
The provider key stays in the lodge. This tool sends text only and never
falls back to Telegram or Slack. Keep the complete formatted message under
4096 characters / 16 KiB UTF-8.

After an ambiguous outcome, do not resend through the tool or CLI: delivery
may already have happened. The worker refuses further sends until a fresh
launch. A stale/unavailable tool means the session binding needs attention;
report that state rather than trying a different destination. If the tool
is absent, the existing CLI instructions below still apply. Tool availability
does not grant general shell/network permission.

## Usage

```bash
# Default — auto-routes via session registry, falls back to Telegram
notify <<'WOLTSPACE_NOTIFY_7F3A91C2'
your message here
WOLTSPACE_NOTIFY_7F3A91C2

# Explicit Slack — send to a specific Slack channel + thread
notify --slack CHANNEL THREAD_TS <<'WOLTSPACE_NOTIFY_4D8E20B1'
your message here
WOLTSPACE_NOTIFY_4D8E20B1

# Explicit Telegram — send to a specific Telegram chat
notify --telegram CHAT_ID <<'WOLTSPACE_NOTIFY_A6C195E4'
your message here
WOLTSPACE_NOTIFY_A6C195E4
```

Use a fresh random suffix in the delimiter for every message, and quote the
opening delimiter exactly as shown. Put the closing delimiter on a line by
itself. Never paste the bare closing delimiter on its own line into the message
body, because that would end the heredoc early. A single-quoted heredoc passes
its body literally: the shell does not expand backticks, `$()`, variables,
quotes, or other metacharacters.

`notify` accepts no message argument. This keeps message text out of the shell
command itself on every harness.

## Local API access and normal approval

`notify` calls the session's configured local Woltspace API; the lodge performs
provider delivery. The model's shell does not need direct WhatsApp access.
If sandbox networking blocks that local API before a send, request normal
one-time approval for the same notify command through the shell tool's
escalation mechanism, explaining that it needs to reach the local lodge API.
Keep the exact route and literal standard input. For Codex shell tools this
means `sandbox_permissions="require_escalated"` with a concrete justification.
For a known blocked environment, request approval before the first send.

An approved command runs outside the command sandbox for that execution;
this is not an exact-port permission. Preserve the existing reviewer and
approval policy. Do not add a persistent prefix grant, enable networking or a
proxy, change sandbox settings, or answer approval dialogs automatically.
If approval is unavailable or denied, report the blocker. If waiting, tell
the user that approval is needed. Existing feature authorization does not
replace runtime approval. A timeout or otherwise ambiguous send outcome must
not trigger a blind resend: the lodge may already be delivering the message.

## Send a file

```bash
notify --file wolt/sparks/weekly-update.html <<'WOLTSPACE_NOTIFY_7F3A91C2'
Weekly update is ready. Open the attached page.
WOLTSPACE_NOTIFY_7F3A91C2
```

`--file PATH` sends one file to the same chat or thread the message goes to.
`PATH` is absolute or relative to the current directory. On Telegram the file
arrives as a document with your message as its caption; a message too long for
a caption arrives first, and the file follows it. On Slack the file lands in
the thread with your message as its comment.

Telegram reads an audio file's name. One ending in `.ogg`, `.oga` or `.opus`
arrives as a voice note, and one ending in `.mp3` or `.m4a` as audio in
Telegram's player. If Telegram will not take it that way (an `.ogg` that is not
Opus, say), it arrives as a plain file. Any other name is a document.

- One file per call, up to 50 MB. Send several files with several calls.
- A file goes only to this lodge's own chats: the chat your session was started
  from, a Telegram chat in `TELEGRAM_ALLOWED_USERS`, or the Slack channel in
  `SLACK_NOTIFY_CHANNEL`. Your own chat always works. To any other chat the
  send is refused and nothing goes out; send text there. On a lodge with no
  `SLACK_NOTIFY_CHANNEL`, a file reaches Slack only from a session that was
  started in Slack.
- `--file PATH` goes before or after the route flags, so you can add it to the
  heredoc Woltspace generates for a reply.
- An HTML page must be one self-contained file, with its styles, scripts and
  images inlined. Nothing it links to on disk travels with it.
- Credential files are refused: `.env`, the Claude and Codex credential files,
  anything in an `.ssh` folder except public keys (`*.pub`), `known_hosts`,
  `authorized_keys` and `config`, SSH private keys by name (`id_rsa`,
  `id_ed25519` and the like) wherever they are, files ending in `.pem`, `.key`,
  `.p12` or `.pfx`, `.netrc`, `.git-credentials` and `.pgpass`, AWS
  `credentials` and `config`, password vaults (`.kdbx` and the like), and
  anything in the lodge's `.space` folder (chat history, sessions, tunnel
  state; to send back a photo the human uploaded, copy it into your wolt's
  folder first). A link to
  one of them under another name is refused too. A Keynote deck also ends in
  `.key`: export it to PDF and send that.
- The message may be empty when a file is attached.

A large file takes as long to send as it takes to upload, so let the command
finish. If it times out, do not send again without checking: the lodge may
still be delivering the file, and a second send would give the user two.

On Slack the app needs the `files:write` scope. Without it the send fails with
a message that names the scope; tell the user, and send the text on its own.

## Send a voice note

```bash
notify --speak <<'WOLTSPACE_NOTIFY_5C3E81D7'
The build is green. I pushed nothing yet.
WOLTSPACE_NOTIFY_5C3E81D7
```

`--speak` has the lodge read your message aloud and send the audio to the same
chat or thread the message goes to. On Telegram the user gets a voice note with
your message as its caption; a message too long for a caption arrives first,
and the voice note follows it. On Slack the audio lands in the thread as a
file, with your message as its comment.

Use it when the user spoke to you (their message starts with `[voice message]`)
or asked for a spoken answer, and the answer is short. Send anything long or
technical as text.

- Write for the ear: plain sentences, no markdown, no lists, no code, no paths
  or links. A few sentences is right. 4096 characters is the hard limit: a
  longer message is refused, not cut.
- The lodge says your words only, not your name in front of them and not the
  reply footer.
- `--voice PATH` sends audio you already have as a voice note: a file ending in
  `.ogg` (Opus), `.oga`, `.opus`, `.mp3` or `.m4a`, up to 50 MB. It is the way
  to ask for a voice note and be told when Telegram cannot make it one. With
  `--file` the same audio still arrives, as a plain file if need be, and you
  are not told which. Any other audio (`.wav`, say) goes with `--file`, as a
  document.
- One of `--file`, `--voice` and `--speak` per call. Like `--file`, the voice
  flags go before or after the route flags.
- Like a file, a voice note and a spoken message go only to the lodge's own
  chats. Another chat gets text only, and nothing is spoken for it.
- If the speech service fails (the lodge tries it twice), the lodge sends your
  words as text with a short "voice note failed" line, and `notify` says
  `the lodge sent the text instead`. The user has your words; do not send them
  again.
- If it fails, send the same message again as text. The error says why: no
  key for the speech service in the lodge's `.env`, the audio was too large, or the
  user has voice messages blocked. If it says the text was delivered, the user
  already has your words; do not send them twice.
- The text of the message goes to the lodge's speech service: OpenAI by
  default (one API request per `--speak`, billed to the lodge's key), or
  whatever the owner set with `woltspace speech set`, such as a model on this
  machine. `woltspace speech show` tells you which.

Speech takes a few seconds to make, more for a long message, so let the command
finish here too. If it times out, check before you send the text: the lodge may
still be delivering the voice note.

## Explicit routing

When your session receives a message from Slack or Telegram, the prepended context includes the routing info you need:

```
[slack message from human, channel=C0123ABC, thread=1711234567.890123]: hey do the thing
Reply by replacing YOUR_REPLY in this exact single-quoted heredoc, then run it:
notify --slack C0123ABC 1711234567.890123 <<'WOLTSPACE_NOTIFY_4D8E20B1'
YOUR_REPLY
WOLTSPACE_NOTIFY_4D8E20B1
```

```
[telegram message from human, chat_id=98765432]: hey do the thing
Reply by replacing YOUR_REPLY in this exact single-quoted heredoc, then run it:
notify --telegram 98765432 <<'WOLTSPACE_NOTIFY_A6C195E4'
YOUR_REPLY
WOLTSPACE_NOTIFY_A6C195E4
```

**Use the exact heredoc from the prepended context and replace only `YOUR_REPLY`.**
Woltspace generates a fresh delimiter every time. This ensures the message stays
literal and goes to the specific Slack thread or Telegram chat it came from;
you do not need to load this skill merely to reply.

## When to use it

- Task finished → tell the user what got built and where to find it
- Something interesting found mid-task → don't wait until the end
- Blocked and need input → ask directly instead of silently stalling
- Long-running work → check in periodically so the user isn't polling

## Examples

```bash
# Task done
notify <<'WOLTSPACE_NOTIFY_08D741BE'
playlist built — 12 tracks, Karkwa + kin. https://open.spotify.com/playlist/xyz
WOLTSPACE_NOTIFY_08D741BE

# Blocked
notify <<'WOLTSPACE_NOTIFY_B2A563F9'
hit a rate limit on Spotify API — should I retry in 30s or skip?
WOLTSPACE_NOTIFY_B2A563F9

# Explicit Slack reply
notify --slack C0123ABC 1711234567.890123 <<'WOLTSPACE_NOTIFY_94C8D130'
done — PR opened at github.com/...
WOLTSPACE_NOTIFY_94C8D130

# Explicit Telegram reply
notify --telegram 98765432 <<'WOLTSPACE_NOTIFY_E70A249C'
found the bug, fix pushed
WOLTSPACE_NOTIFY_E70A249C
```

## How it works

`notify` POSTs to `localhost:7777/notify` → server sends via Telegram/Slack API directly. Synchronous, no polling delay.

With explicit flags (`--slack`, `--telegram`), the server skips session registry lookup entirely and sends directly to the specified target. Without flags, it falls back to session-based routing, then Telegram default.
