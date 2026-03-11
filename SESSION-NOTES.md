# Session Notes — Agent Loop Rewrite (2026-03-11)

## What we did

Rewrote the bot agent loop and added a den routing protocol. All changes are
on the `agent-loop` branch (4 commits).

### 1. Agent loop rewrite (`container/bot/core.py`)
- **Multi-turn tool loop** — Haiku can now chain tools (e.g. check_session then
  read_memory then respond). Max 5 rounds per turn.
- **All tool calls preserved in history** — previously only `claude_code` stored
  tool_call/tool_result in history. Other tools (check_session, get_tunnel_url,
  etc.) were collapsed into plain assistant text, which made Haiku think it
  "knew" answers without calling tools and stop using them.
- **Tool handler registry** — clean `TOOL_HANDLERS` dict instead of if/elif chain.
- **Fixed SESSION_STATUS_DIR bug** — was frozen at import, now dynamic via
  `_session_status_dir()` so it follows `switch_wolt`.

### 2. Den routing protocol (`server.js`, `telegram_adapter.py`)
- **Den messages stored as `role: "user"`** with `<system>` context tag instead
  of `role: "assistant"`. Haiku sees them as context, not its own speech.
- **DEN_REPLY_FOOTER sentinel** — `↩️ reply to this message to talk to this
  session directly` appended to Telegram notify messages, stripped from history.
  Haiku never sees it and can't reproduce it.
- **Reply-to-den routing** — when human replies to a 🦫 message (detected by
  footer presence), the reply goes directly to the Claude Code session via
  `message_session()`, bypassing Haiku entirely. Stored in history as context-only.
- **Notify wording** — den sessions now instructed to include actual substance
  in notify messages (telegram-friendly summary), not just "done, see session."

### 3. Adapter updates (`telegram_adapter.py`, `slack_adapter.py`)
- Both adapters now use `history_messages` uniformly for all result types
  (not just session starts).

## Key files

| File | What |
|---|---|
| `container/bot/core.py` | Agent loop, tool registry, system prompt |
| `container/bot/telegram_adapter.py` | Den reply detection, history storage |
| `container/bot/slack_adapter.py` | History storage (same pattern) |
| `container/bot/AGENT-LOOP.md` | **Full architecture doc** — message flows, history format, sentinel protocol, examples |
| `container/bin/run-session.sh` | Notify instructions for den sessions |
| `server.js` | `appendChatHistory` (den→history), `sendNotification` (footer), `DEN_REPLY_FOOTER` |

## Where to continue debugging

1. **Rebuild the container** to pick up all changes (server.js, bot, run-session.sh)
2. Send a message via Telegram, trigger a session, wait for `notify`
3. Check chat history: `docker exec woltspace bash -c "tail -20 /workspace/wolts/neowolt/.state/chat/8547543098.jsonl"` — den messages should now be `role: "user"` with `<system>` tag
4. Check that the Telegram message has the `↩️ reply to...` footer
5. Reply to a 🦫 message and verify it goes to the session (check bot logs for `den_reply` event)
6. Bot debug log: `docker exec woltspace bash -c "tail -50 /workspace/wolts/.state/bot-debug/bot.jsonl"`

## TODO — next sessions

- [ ] **Reliable restart without rebuild** — `node --watch` doesn't trigger on
  Docker-mounted volumes (filesystem events don't propagate). The Python bot
  has no file watcher at all. Need a solution: either `watchfiles` (Python) +
  fix node watch for mounts, or a simple `make restart-bot` / `make restart-server`
  that kills and relaunches cleanly.
- [ ] **Verify den routing end-to-end** — after rebuild, confirm:
  - 🦫 messages have footer in Telegram
  - 🦫 messages stored as `role: "user"` in JSONL
  - Replying to 🦫 routes to session (not Haiku)
  - Haiku doesn't parrot or respond to `<system>` tagged messages
- [ ] **Slack adapter den routing** — only Telegram has reply-to-den detection.
  Slack threading makes this different (thread replies already have context).
  Decide if needed.
- [ ] **DEN_REPLY_FOOTER sync** — the sentinel string is defined in two places
  (`server.js` and `telegram_adapter.py`). Could be a shared config or env var.
- [ ] **Old history cleanup** — existing chat JSONL files have old-format messages
  (den messages as `role: "assistant"`, tool calls collapsed to text). These
  will confuse Haiku until they scroll out of the history window (MAX_HISTORY=20
  message pairs). Could manually clean or just let them age out.
