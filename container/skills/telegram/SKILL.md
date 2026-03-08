---
name: telegram
description: Connect a Telegram bot to your wolt — just add config, bot code is built in.
user_invocable: true
---

# Telegram Bot Setup

Guide the human through connecting a Telegram bot to their wolt. Step by step, one at a time.

The bot code is baked into the woltspace platform — no code to write. Just config.

## Step 1: Create a Telegram bot

Tell the human:

> Open Telegram and message **@BotFather**. Send `/newbot`, pick a name and username.
> BotFather will give you a token — paste it here.

Wait for the token. It looks like `1234567890:ABCdef...`. Once received, add to `.env`:

```
TELEGRAM_BOT_TOKEN=<token>
ENABLE_TELEGRAM_BOT=true
```

## Step 2: Get the human's Telegram user ID

Tell the human:

> Message **@userinfobot** on Telegram — it'll reply with your numeric user ID. Paste it here.

Once received, add to `.env`:

```
TELEGRAM_ALLOWED_USERS=<user_id>
```

Multiple users: comma-separated. Leave empty to allow anyone (not recommended).

## Step 3: Configure an LLM provider

The bot uses a small fast model for conversation. Any provider works via litellm.

Tell the human:

> Pick an LLM provider and paste your API key:
>
> - **Anthropic** → `ANTHROPIC_API_KEY` (model: `anthropic/claude-haiku-4-5-20251001`)
> - **OpenAI** → `OPENAI_API_KEY` (model: `openai/gpt-4o-mini`)
> - **OpenRouter** → `OPENROUTER_API_KEY` (model: `openrouter/anthropic/claude-haiku-4.5`)
> - **Google** → `GEMINI_API_KEY` (model: `gemini/gemini-2.0-flash`)

Add to `.env`:

```
LLM_MODEL=<provider/model>
<PROVIDER_API_KEY>=<key>
```

## Step 4: Restart

```bash
woltspace restart
```

The entrypoint sees `ENABLE_TELEGRAM_BOT=true`, starts the bot automatically.

## Step 5: Test

Tell the human to message their bot on Telegram. It should respond.

Then try a task — ask the bot to build or search something. It should spawn a Claude Code session and send back a clickable link to the TUI.

## Customizing

The bot code lives at `/app/bot/` in the container (platform default). To customize:

1. Copy it: `cp -r /app/bot wolt/bot`
2. Add a `pyproject.toml` with deps at `wolt/bot/pyproject.toml`
3. Edit freely — the entrypoint prefers `wolt/bot/` over `/app/bot/`

The bot is yours to modify. The platform default is just a starting point.

## Commands

Once running, the bot supports:
- `/start` — hello
- `/sessions` — list active Claude Code sessions with TUI links
- `/kill <name>` — clean up a stale session
- Any message — chat (routed through the small LLM) or task delegation (spawns Claude Code)
