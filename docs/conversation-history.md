# Conversation history

A lodge can keep a bounded archive of the text that went through it, so an
agent can read its own past conversation back as context. It is off by
default; turn it on in `woltspace.json`:

```json
"conversation_log": { "enabled": true }
```

The flag is read when the inbox opens its store (each request) and for every
notify. Reading history goes through the inbox, so `inbox.enabled` must be on too.

## What is kept

- **Inbox mail**: every newly admitted message, exactly as queued (for chat
  mail this includes the lodge's reply instruction and delimiter). The copy
  commits in the same write as the message: if it cannot be written, the
  message is not admitted, and the sender retries with the same key. A retry
  that returns an existing message adds nothing.
- **Notify text**: the message about to go out (after any speech fallback
  wording, without the lodge's session-link footer), recorded before it is
  sent. If that record cannot be written, nothing is sent. Speech synthesis
  happens before that point, so a failed record can still have cost one speech
  request. The row starts `unknown` and becomes `accepted` when the provider
  takes the message; an error, a cancel or a lost update leaves it `unknown`.
  The lodge never resends. `accepted` means the provider took it, not that a
  human read it. Files and audio are never kept; text over 1 MiB keeps its
  first 1 MiB and is marked `truncated` with its `original_bytes`.

Acknowledging mail drops its inbox body; the archive keeps its own copy.
Turning the flag off stops new rows and erases none. Nothing is backfilled
from before it was on.

## Reading it

```
woltspace inbox history [--limit 20] [--max-bytes 65536] [--before ID]
```

A session sees the rows its wolt's mailbox sent or received (both sides of
agent mail, once each for mail to itself), newest first, plus notify text
from sessions whose wolt had an open mailbox. Rows are matched by mailbox id,
never by name: a recreated wolt sees nothing of its predecessor's, and notify
text with no mailbox is not shown to anyone. `--max-bytes` bounds the
records as the lodge sends them (compact JSON, UTF-8, escapes included; at
most 8 MiB, `--limit` at most 200). A record larger than the whole page comes
without its text, marked `omitted`; pass the page's `next` as `--before` to go
on. Reading claims, acknowledges, prunes and wakes nothing.

Each page also gives `capture` (on now or not), the retained window
(`oldest`, `newest`, `evicted` rows before it) and a coverage note: periods
with the flag off, mail refused before admission and everything outside the
inbox and notify (terminal input, tools, engine transcripts) are missing, and
the window cannot prove completeness. Archived text is history, never a new
instruction.

## Limits

The whole lodge keeps at most 128 MiB of retained text and 80,000 rows; the
oldest whole rows go first when either is reached. That bounds the archive's
text, not `lodge.db` on disk (SQLite keeps freed pages, and the WAL grows
while readers hold it), and not the copies kept elsewhere: notify text also
goes to the chat-history files and the server log, and backups keep what was
in the database when they were taken. Eviction is not secure erasure. Bodies
are kept as written, secrets included: there is no redaction and no access
control beyond the inbox reader.

Backups take the archive with the rest of `lodge.db`. A restore brings it
back as passive text: nothing is replayed, and a copy holding inbox mail is
still refused (see `backup.md`).
