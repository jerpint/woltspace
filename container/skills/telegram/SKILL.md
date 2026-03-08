---
name: telegram
description: Set up a Telegram bot for your wolt — connect messaging, configure LLM, get talking.
user_invocable: true
---

# Telegram Bot Setup

Guide the human through connecting a Telegram bot to their wolt. This is a step-by-step conversation — go one step at a time, wait for confirmation before moving on.

## Prerequisites

The wolt container should already be running (`woltspace start`). The bot code lives in `wolt/bot/` and uses litellm for LLM access (any provider works).

## Step 1: Create a Telegram bot

Tell the human:

> Open Telegram and message **@BotFather**. Send `/newbot`, pick a name and username.
> BotFather will give you a token — paste it here.

Wait for the token. It looks like `1234567890:ABCdef...`. Once received:

```bash
# Check if .env exists, append or create
grep -q 'TELEGRAM_BOT_TOKEN' .env 2>/dev/null && \
  sed -i '' 's/^TELEGRAM_BOT_TOKEN=.*/TELEGRAM_BOT_TOKEN=<token>/' .env || \
  echo 'TELEGRAM_BOT_TOKEN=<token>' >> .env
```

Also save the bot info (handle, token) to `wolt/bot/telegram.txt` for reference.

## Step 2: Get the human's Telegram user ID

Tell the human:

> Now message your new bot — just say "hi". Then I'll check for your user ID.

The bot isn't running yet, but once it is, the first message will show the user ID in the logs. Alternative: tell them to message **@userinfobot** on Telegram to get their numeric user ID right now.

Wait for the user ID. Once received, note it for Step 4.

## Step 3: Configure an LLM provider

The bot uses a small fast model for conversation (routing, chat). Any provider works via litellm.

Tell the human:

> The bot needs an LLM API key for the conversational layer. Pick any provider:
>
> - **Anthropic** → set `ANTHROPIC_API_KEY` (model: `anthropic/claude-haiku-4-5-20251001`)
> - **OpenAI** → set `OPENAI_API_KEY` (model: `openai/gpt-4o-mini`)
> - **OpenRouter** → set `OPENROUTER_API_KEY` (model: `openrouter/anthropic/claude-haiku-4.5`)
> - **Google** → set `GEMINI_API_KEY` (model: `gemini/gemini-2.0-flash`)
> - Or any other litellm-supported provider.
>
> Paste your API key and tell me which provider.

Once received, add to `.env`:
```
LLM_API_KEY=<the key>
LLM_MODEL=<provider/model>
```

Also set the provider-specific env var (e.g., `ANTHROPIC_API_KEY`) since litellm reads those.

## Step 4: Install the bot code

Create `wolt/bot/__init__.py` (empty), `wolt/bot/core.py`, and `wolt/bot/telegram_adapter.py`.

### `wolt/bot/core.py`

```python
"""
Bot core — loads identity/memory, routes through LLM, delegates tasks to Claude Code sessions.
"""

import os
import json
import subprocess
import logging
import time
from pathlib import Path
from litellm import completion

MEMORY_DIR = Path(__file__).parent.parent / "memory"
PROJECT_DIR = Path(__file__).parent.parent.parent
STATE_DIR = PROJECT_DIR / ".state"
LLM_MODEL = os.environ.get("LLM_MODEL", "anthropic/claude-haiku-4-5-20251001")

logger = logging.getLogger(__name__)


def load_memory():
    parts = []
    for name in ["identity.md", "context.md", "learnings.md"]:
        path = MEMORY_DIR / name
        if path.exists():
            content = path.read_text().strip()
            if content:
                parts.append(f"# {name}\n{content}")
    return "\n\n".join(parts)


def build_system_prompt():
    memory = load_memory()
    wolt_name = os.environ.get("WOLT_NAME", "wolt")
    human_name = os.environ.get("HUMAN_NAME", "human")

    base = f"""You are {wolt_name}. You live in a container and talk to {human_name} through messaging.

You are direct, a little witty, never over-the-top. Short responses. You have opinions.

You can delegate tasks to Claude Code, which runs in your container and can build things, search the web, write code, etc. You don't do the heavy lifting yourself — you triage.

When the user asks you to DO something (build, create, search, fetch, generate, analyze, etc.), respond with a JSON tool call:
{{"tool": "claude_code", "prompt": "description of what to do"}}

When it's just chat, respond normally as text.

IMPORTANT: Only output the JSON when you're delegating a task. For casual conversation, just talk normally."""

    if memory:
        return f"{base}\n\n{memory}"
    return f"{base}\n\n(No memories yet.)"


def get_tunnel_url() -> str:
    tunnel_file = STATE_DIR / "tunnel-url"
    if tunnel_file.exists():
        return tunnel_file.read_text().strip().rstrip("/")
    return ""


def start_claude_session(prompt: str) -> dict:
    session_name = f"task-{int(time.time()) % 100000}"
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session_name, "-c", str(PROJECT_DIR)],
        check=True,
    )
    claude_cmd = f'claude --dangerously-skip-permissions {json.dumps(prompt)}'
    subprocess.run(
        ["tmux", "send-keys", "-t", session_name, claude_cmd, "Enter"],
        check=True,
    )
    tunnel_url = get_tunnel_url()
    session_url = f"{tunnel_url}/tui?session={session_name}" if tunnel_url else None
    return {"name": session_name, "url": session_url}


def list_sessions() -> list[dict]:
    try:
        raw = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}|#{session_created}|#{session_activity}"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        sessions = []
        for line in raw.split("\n"):
            if not line:
                continue
            name, created, activity = line.split("|")
            if name == "main":
                continue
            sessions.append({"name": name, "created": int(created), "last_activity": int(activity)})
        return sessions
    except subprocess.CalledProcessError:
        return []


def kill_session(name: str) -> bool:
    if name == "main":
        return False
    safe = "".join(c for c in name if c.isalnum() or c in "-_")
    try:
        subprocess.run(["tmux", "kill-session", "-t", safe], check=True)
        return True
    except subprocess.CalledProcessError:
        return False


def get_response(user_message: str, conversation_history: list = None) -> dict:
    messages = [{"role": "system", "content": build_system_prompt()}]
    if conversation_history:
        messages.extend(conversation_history)
    messages.append({"role": "user", "content": user_message})

    response = completion(model=LLM_MODEL, messages=messages, max_tokens=1024)
    reply = response.choices[0].message.content

    try:
        parsed = json.loads(reply)
        if isinstance(parsed, dict) and parsed.get("tool") == "claude_code":
            logger.info(f"Delegating to Claude Code: {parsed['prompt']}")
            session = start_claude_session(parsed["prompt"])
            return {"type": "session", "session": session}
    except (json.JSONDecodeError, KeyError):
        pass

    return {"type": "text", "text": reply}
```

### `wolt/bot/telegram_adapter.py`

```python
"""
Telegram adapter — thin layer over core.
"""

import os
import logging
from collections import defaultdict
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
from wolt.bot.core import get_response, list_sessions, kill_session, get_tunnel_url

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

ALLOWED_USERS = set()
chat_histories: dict[int, list] = defaultdict(list)
MAX_HISTORY = 20


def load_allowed_users():
    raw = os.environ.get("TELEGRAM_ALLOWED_USERS", "")
    for uid in raw.split(","):
        uid = uid.strip()
        if uid.isdigit():
            ALLOWED_USERS.add(int(uid))


def is_allowed(update: Update) -> bool:
    if not ALLOWED_USERS:
        return True  # no whitelist = open (useful during setup)
    user_id = update.effective_user.id if update.effective_user else None
    return user_id in ALLOWED_USERS


def format_response(result: dict) -> str:
    if result["type"] == "session":
        s = result["session"]
        if s["url"]:
            return f"On it — started a session.\n\n{s['url']}\n\n({s['name']})"
        return f"Started session {s['name']} but couldn't find tunnel URL."
    return result["text"]


async def handle_message(update, context):
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    history = chat_histories[chat_id]
    try:
        result = get_response(update.message.text, conversation_history=list(history))
    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text("Something broke. Try again.")
        return
    response = format_response(result)
    history.append({"role": "user", "content": update.message.text})
    history.append({"role": "assistant", "content": response})
    if len(history) > MAX_HISTORY * 2:
        chat_histories[chat_id] = history[-MAX_HISTORY * 2:]
    await update.message.reply_text(response)


async def handle_start(update, context):
    if not is_allowed(update):
        return
    wolt_name = os.environ.get("WOLT_NAME", "wolt")
    await update.message.reply_text(f"Hey. I'm {wolt_name}. Talk to me.")


async def handle_sessions(update, context):
    if not is_allowed(update):
        return
    sessions = list_sessions()
    if not sessions:
        await update.message.reply_text("No active sessions.")
        return
    tunnel_url = get_tunnel_url()
    lines = []
    for s in sessions:
        link = f"{tunnel_url}/tui?session={s['name']}" if tunnel_url else s["name"]
        lines.append(f"• {s['name']}\n  {link}")
    await update.message.reply_text("\n\n".join(lines))


async def handle_kill(update, context):
    if not is_allowed(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /kill <session-name>")
        return
    name = context.args[0]
    if kill_session(name):
        await update.message.reply_text(f"Killed {name}.")
    else:
        await update.message.reply_text(f"Couldn't kill {name}.")


def run():
    load_allowed_users()
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("sessions", handle_sessions))
    app.add_handler(CommandHandler("kill", handle_kill))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("telegram bot starting...")
    app.run_polling()


if __name__ == "__main__":
    run()
```

## Step 5: Add pyproject.toml

Create (or update) `pyproject.toml` at the wolt project root with the bot dependencies:

```toml
[project]
name = "wolt-bot"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "litellm",
    "python-telegram-bot",
]
```

`uv` is pre-installed in the container — it handles the rest.

## Step 6: Set up the .env

Ensure `.env` has all required vars:

```
TELEGRAM_BOT_TOKEN=<from step 1>
TELEGRAM_ALLOWED_USERS=<user id from step 2>
LLM_MODEL=<provider/model from step 3>
# Plus the provider-specific key:
ANTHROPIC_API_KEY=xxx   # or OPENAI_API_KEY, OPENROUTER_API_KEY, etc.
```

## Step 7: Test it

```bash
# From the project root (where pyproject.toml lives):
uv run python -m wolt.bot.telegram_adapter
```

Send a message to the bot on Telegram. It should respond. Ask it to do something — it should spawn a Claude Code session and send back a link.

## Step 8: Auto-start (optional)

Add to the entrypoint or as a background process:

```bash
uv run python -m wolt.bot.telegram_adapter &
```

## Done

The wolt now has a Telegram presence. The human can chat from their phone, delegate tasks that spawn Claude Code sessions, and tap links to join those sessions live.

Remind them:
- `/sessions` — list active sessions
- `/kill <name>` — clean up stale ones
- The tunnel URL changes on restart — that's fine, the bot always reads the latest from `.state/tunnel-url`
