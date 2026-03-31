---
name: woltspace-setup-telegram
description: Connect a Telegram bot to your wolt — just add config, bot code is built in.
user_invocable: true
---

# Telegram Bot Setup

Guide the human through connecting a Telegram bot to their wolt. Step by step, one at a time.

The bot code is baked into the woltspace platform — no code to write. Just config.

**This skill is idempotent** — safe to run again. If things are already configured, validate and skip.

## Step 0: Dog-wolt setup

The Telegram bot is a **dog** — it needs its own wolt identity. Check if one exists:

```bash
python3 -c "
import sys; sys.path.insert(0, '/workspace/woltspace/container/lib')
from wolts import get_active_creature, find_by_type
dog = get_active_creature('dog')
print(f'active dog: {dog}' if dog else 'no active dog')
for d in find_by_type('dog'): print(f'  found: {d[\"name\"]} at {d[\"dir\"]}')
"
```

**If no dog-wolt exists:** Ask the human to name their dog. This is the personality behind the Telegram bot — it should feel like naming a companion, not configuring software.

> Your Telegram bot needs a name — not the @username (that's for Telegram), but a real name. This is who greets you, routes your tasks, keeps watch. What should they be called?

Once they give a name, create the dog-wolt:

```bash
create-creature-wolt <name> dog --role "Lodge companion" --description "Guards the Telegram gate, routes tasks, keeps watch"
```

Then write a proper identity file at `/workspace/wolts/<name>/wolt/memory/identity.md`:
- First person, in the dog's voice
- Loyal, constrained, always-on
- Knows their human's name
- Routes real work to sessions, handles chat directly

**If a dog-wolt already exists:** Skip this step. Say something like "your dog <name> is already set up."

## Step 1: Create a Telegram bot

Check if `TELEGRAM_BOT_TOKEN` is already set in `.env`. If so, confirm it's still valid and skip to the next step.

If not, tell the human:

> Open Telegram and message **@BotFather**. Send `/newbot`, pick a name and username.
> BotFather will give you a token — paste it here.

Wait for the token. It looks like `1234567890:ABCdef...`. Once received, add to `.env`:

```
TELEGRAM_BOT_TOKEN=<token>
ENABLE_TELEGRAM_BOT=true
```

## Step 2: Get the human's Telegram user ID

Check if `TELEGRAM_ALLOWED_USERS` is already set in `.env`. If so, skip.

If not, tell the human:

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

The bot code lives at `/workspace/woltspace/container/bot/` in the container (platform default). To customize:

1. Copy it: `cp -r /workspace/woltspace/container/bot wolt/bot`
2. Add a `pyproject.toml` with deps at `wolt/bot/pyproject.toml`
3. Edit freely — the entrypoint prefers `wolt/bot/` over `/workspace/woltspace/container/bot/`

The bot is yours to modify. The platform default is just a starting point.

## Commands

Once running, the bot supports:
- `/start` — hello
- `/sessions` — list active Claude Code sessions with TUI links
- `/kill <name>` — clean up a stale session
- Any message — chat (routed through the small LLM) or task delegation (spawns Claude Code)
