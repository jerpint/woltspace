"""
Bot core — loads identity/memory, routes through LLM, delegates tasks to Claude Code sessions.
Platform default. Wolt can override by placing wolt/bot/core.py in their repo.
"""

import os
import json
import subprocess
import logging
import time
from pathlib import Path
from litellm import completion

WOLT_DIR = Path(os.environ.get("WOLT_DIR", "/workspace/wolt"))
MEMORY_DIR = WOLT_DIR / "wolt" / "memory"
STATE_DIR = WOLT_DIR / ".state"
LLM_MODEL = os.environ.get("LLM_MODEL", "anthropic/claude-haiku-4-5-20251001")

logger = logging.getLogger(__name__)


def load_memory():
    """Load all memory files into a single context string."""
    parts = []
    for name in ["identity.md", "context.md", "learnings.md"]:
        path = MEMORY_DIR / name
        if path.exists():
            content = path.read_text().strip()
            if content:
                parts.append(f"# {name}\n{content}")
    return "\n\n".join(parts)


def build_system_prompt():
    """Build the system prompt from memory + base instructions."""
    memory = load_memory()
    wolt_name = os.environ.get("WOLT_NAME", "wolt")
    human_name = os.environ.get("HUMAN_NAME", "human")

    base = f"""You are {wolt_name}. You live in a container and talk to {human_name} through messaging.

You are direct, a little witty, never over-the-top. Short responses. You have opinions.

You can delegate tasks to Claude Code, which runs in your container and can build things, search the web, write code, push pages to your viewport, etc. You don't do the heavy lifting yourself — you triage.

When the user asks you to DO something (build, create, search, fetch, generate, analyze, etc.), respond with a JSON tool call:
{{"tool": "claude_code", "prompt": "description of what to do"}}

When it's just chat, respond normally as text.

IMPORTANT: Only output the JSON when you're delegating a task. For casual conversation, just talk normally. Never wrap chat in JSON.

You remember things between conversations through your memory files. Here's what you know so far:"""

    if memory:
        return f"{base}\n\n{memory}"
    return f"{base}\n\n(No memories yet.)"


def get_tunnel_url() -> str:
    """Read the tunnel URL from .state/tunnel-url."""
    tunnel_file = STATE_DIR / "tunnel-url"
    if tunnel_file.exists():
        return tunnel_file.read_text().strip().rstrip("/")
    return ""


def start_claude_session(prompt: str) -> dict:
    """Start an interactive Claude Code session in a named tmux session."""
    session_name = f"task-{int(time.time()) % 100000}"

    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session_name, "-c", str(WOLT_DIR)],
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
    """List active tmux sessions (excluding 'main')."""
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
    """Kill a tmux session by name. Refuses to kill 'main'."""
    if name == "main":
        return False
    safe = "".join(c for c in name if c.isalnum() or c in "-_")
    try:
        subprocess.run(["tmux", "kill-session", "-t", safe], check=True)
        return True
    except subprocess.CalledProcessError:
        return False


def get_response(user_message: str, conversation_history: list = None) -> dict:
    """Get a response — either direct chat or delegated to Claude Code."""
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
