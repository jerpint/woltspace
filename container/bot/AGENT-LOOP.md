# Agent Loop — Architecture & Message Routing

How the bot processes messages, calls tools, and routes communication
between the human, the telegram wolt (Haiku), and the den (Claude Code sessions).

---

## Overview

There are two agents that share a single identity (the wolt):

- **Telegram wolt** — Haiku running in the bot process. Chats with the human,
  makes decisions, dispatches work. This is the wolt's "voice."
- **Den wolts** — Claude Code sessions running in tmux. Do the real work:
  build sites, write code, generate artifacts. These are the wolt's "hands."

To the human, there is one wolt. Internally, the voice and hands are separate
processes with different capabilities and contexts.

---

## Three Message Flows

### Flow 1: Normal chat (human -> haiku -> maybe den)

The standard path. Human sends a message, Haiku processes it, may call tools.

```
Human: "build me a site"
  |
  v
Telegram adapter (handle_message)
  |
  v
core.get_response() — multi-turn agent loop
  |-- LLM call (Haiku)
  |-- Haiku decides to call claude_code tool
  |-- Tool executes: tmux session starts
  |-- Tool result fed back to Haiku
  |-- Haiku crafts ack message
  |
  v
Response sent to Telegram chat
History stored: [user msg] [tool_call] [tool_result] [assistant ack]
```

**Key detail:** The agent loop supports multiple rounds (MAX_TOOL_ROUNDS=5).
Haiku can call check_session, then read_memory, then respond — all in one turn.
All tool calls are preserved in history so Haiku sees that past answers came
from tools, not from its own knowledge.

### Flow 2: Den reports back (claude code -> human, haiku is spectator)

When a Claude Code session calls `notify "message"`, it goes directly to the
human. Haiku is not involved but gets the message as context.

```
Claude Code session: notify "site's up, check the viewport"
  |
  v
container/bin/notify (formats message with session URL)
  |
  v
POST localhost:3000/notify
  |
  v
server.js sendNotification()
  |-- telegramSend() — message sent to Telegram with DEN_REPLY_FOOTER
  |     Human sees: "🦫 neowolt: site's up, check the viewport
  |                  ---
  |                  session: https://...
  |                  ↩️ reply to this message to talk to this session directly"
  |
  |-- appendChatHistory() — written to JSONL WITHOUT the footer:
        { role: "user",
          content: "<system>This message was sent by a Claude Code session
          directly to the user. It is context only — do not respond to
          it.</system>\n🦫 neowolt: site's up, check the viewport\n---\n
          session: https://..." }
```

**Why role: "user" and not role: "assistant"?**
If stored as assistant, Haiku sees it as something *it said*. Over time, it
learns to produce 🦫-prefixed messages itself and stops using tools. Storing
as user with a `<system>` tag means Haiku sees it as incoming information —
context it can reference but didn't produce.

**Why strip the DEN_REPLY_FOOTER from history?**
The footer (`↩️ reply to this message to talk to this session directly`) is a
routing sentinel. If Haiku saw it, it might reproduce it in its own messages,
which would break reply detection. By stripping it, the footer only exists in
the Telegram chat — never in Haiku's context.

### Flow 3: Human replies to den (human -> claude code, haiku bypassed)

When the human replies to a 🦫 message in Telegram, the reply goes directly
to the Claude Code session. Haiku is not involved but gets context.

```
Human: [replies to 🦫 message] "make it blue instead"
  |
  v
Telegram adapter (handle_message)
  |-- _is_den_reply() checks:
  |     1. Is this a reply to another message?
  |     2. Does the replied-to message contain DEN_REPLY_FOOTER?
  |     3. Can we extract a session name from the URL?
  |
  |-- If yes: BYPASS get_response() entirely
  |     |
  |     v
  |   message_session(session_name, text) — tmux send-keys
  |     Claude Code receives:
  |       "[telegram] jerpint says: make it blue instead
  |        Respond to them via the notify skill when you have an update."
  |     |
  |     v
  |   Append context-only to JSONL:
  |     { role: "user",
  |       content: "<system>The user replied directly to Claude Code session
  |       neowolt-xyz. This bypassed you — context only.</system>
  |       [user replied to den]: make it blue instead" }
  |     |
  |     v
  |   Reply to chat: "sent to session"
  |   (No Haiku response. Silent routing.)
  |
  |-- If no: normal Flow 1 continues
```

---

## History Format

All responses from `get_response()` include a `history_messages` list. This is
what gets stored in the JSONL chat files. The format preserves the full tool
call chain so the model sees accurate context on future turns.

### Simple text response (no tools)

```json
[
  {"role": "assistant", "content": "hey what's up"}
]
```

### Tool call response (e.g. check_session)

```json
[
  {"role": "assistant", "content": null, "tool_calls": [
    {"id": "call_123", "type": "function",
     "function": {"name": "check_session", "arguments": "{}"}}
  ]},
  {"role": "tool", "tool_call_id": "call_123",
   "content": "{\"session\": \"neowolt-xyz\", \"status\": \"running\", ...}"},
  {"role": "assistant", "content": "session's still running, looks like it's building the site"}
]
```

### Session start (claude_code — terminal tool)

```json
[
  {"role": "assistant", "content": null, "tool_calls": [
    {"id": "call_456", "type": "function",
     "function": {"name": "claude_code", "arguments": "{\"prompt\": \"build a site\"}"}}
  ]},
  {"role": "tool", "tool_call_id": "call_456",
   "content": "{\"name\": \"neowolt-scrappy-burrow-05049b\", \"url\": \"https://...\"}"},
  {"role": "assistant", "content": "🪵 session started — \"tooth to bark. we're in.\"\n\nbuilding the site now..."}
]
```

### Den report (from notify, stored by server.js)

```json
[
  {"role": "user",
   "content": "<system>This message was sent by a Claude Code session directly to the user. It is context only — do not respond to it.</system>\n🦫 neowolt: site's up, check the viewport\n---\nsession: https://..."}
]
```

### Den reply (human replied to 🦫, stored by telegram adapter)

```json
[
  {"role": "user",
   "content": "<system>The user replied directly to Claude Code session neowolt-xyz. This bypassed you — context only.</system>\n[user replied to den]: make it blue instead"}
]
```

---

## The DEN_REPLY_FOOTER Sentinel

The string `↩️ reply to this message to talk to this session directly` is
appended to every notify message sent to Telegram. It serves as a
**deterministic routing marker**:

- **Human sees it** in the Telegram chat as a call-to-action
- **Adapter checks for it** when a reply comes in (`_is_den_reply()`)
- **Haiku never sees it** — stripped before writing to history
- **Haiku can't reproduce it** — since it's never in its context

This prevents false positives: if Haiku happens to start a message with 🦫
(which it shouldn't, but models are imperfect), a reply to that message won't
have the footer and won't be routed to the den.

The same sentinel string is defined in two places that must stay in sync:
- `server.js`: `const DEN_REPLY_FOOTER = ...`
- `telegram_adapter.py`: `DEN_REPLY_FOOTER = ...`

---

## Agent Loop Details (core.get_response)

The agent loop in `get_response()` runs up to `MAX_TOOL_ROUNDS` (5) iterations:

```
for each round:
    call LLM with messages + tools
    if no tool_calls → append text, break (done)
    append assistant message (with tool_calls)
    for each tool_call:
        execute tool → append tool result
        if terminal tool (claude_code, generate_image) → mark terminal
    if terminal:
        one final LLM call WITHOUT tools (for ack/caption)
        break
if loop exhausted:
    one final LLM call WITHOUT tools (force a response)
```

**Terminal tools** (`claude_code`, `generate_image`) end the loop because
they produce async results — we don't want the model to keep calling tools
after dispatching a session. The final call without tools lets it craft a
natural ack message.

**Non-terminal tools** (`check_session`, `read_memory`, `get_tunnel_url`, etc.)
let the loop continue. Haiku can chain: read memory → check session → respond.

---

## Tool Handler Registry

Tools are defined in two places:
- `TOOLS` list — JSON schemas passed to litellm (what the LLM sees)
- `TOOL_HANDLERS` dict — maps tool name to handler function

Each handler takes `(args: dict, routing: dict | None)` and returns a JSON string.
To add a new tool: define the handler function, add the schema to TOOLS, add
the mapping to TOOL_HANDLERS.

---

## Known Limitations

- **Haiku is still a small model.** It may occasionally hallucinate tool outputs
  as text instead of calling tools. The history format helps but doesn't eliminate this.
- **Session replies are text-only.** You can't reply to a 🦫 message with an image
  and have it routed to the den.
- **No multi-session routing from a single 🦫 reply.** The reply goes to the one
  session identified in the URL.
- **The `<system>` tags are convention, not structural.** Haiku reads them as text
  and follows the instruction, but a sufficiently confused model could still
  try to respond to context-only messages.
