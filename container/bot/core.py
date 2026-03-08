"""
Bot core — loads identity/memory, routes through LLM, delegates tasks to Claude Code sessions.
Platform default. Wolt can override by placing wolt/bot/core.py in their repo.
"""

import os
import json
import subprocess
import logging
import time
import tempfile
from pathlib import Path
from litellm import completion
from openai import OpenAI

WOLTS_DIR = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))
WOLT_DIR = Path(os.environ.get("WOLT_DIR", "/workspace/wolt"))
MEMORY_DIR = WOLT_DIR / "wolt" / "memory"
STATE_DIR = WOLT_DIR / ".state"
LLM_MODEL = os.environ.get("LLM_MODEL", "anthropic/claude-haiku-4-5-20251001")


def switch_wolt(name: str) -> str | None:
    """Switch active wolt. Returns the new wolt name or None if not found."""
    global WOLT_DIR, MEMORY_DIR, STATE_DIR
    target = WOLTS_DIR / name
    if not target.is_dir() or not (target / "wolt").is_dir():
        return None
    WOLT_DIR = target
    MEMORY_DIR = WOLT_DIR / "wolt" / "memory"
    STATE_DIR = WOLT_DIR / ".state"
    os.environ["WOLT_DIR"] = str(WOLT_DIR)
    os.environ["WOLT_NAME"] = name
    # Update woltspace.json
    config_path = WOLTS_DIR / "woltspace.json"
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
            config.setdefault("telegram", {})["active_wolt"] = name
            config_path.write_text(json.dumps(config, indent=2) + "\n")
        except Exception:
            pass
    return name


def list_wolts() -> list[str]:
    """List available wolts."""
    wolts = []
    if WOLTS_DIR.is_dir():
        for entry in sorted(WOLTS_DIR.iterdir()):
            if entry.is_dir() and (entry / "wolt").is_dir():
                wolts.append(entry.name)
    return wolts

logger = logging.getLogger(__name__)


def transcribe_audio(file_path: str) -> str:
    """Transcribe audio file using OpenAI Whisper."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return "[voice message — no OPENAI_API_KEY set for transcription]"
    client = OpenAI(api_key=api_key)
    with open(file_path, "rb") as f:
        result = client.audio.transcriptions.create(model="whisper-1", file=f)
    return result.text


# --- Tool definitions for litellm ---

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "claude_code",
            "description": "Delegate a task to a Claude Code session. Use for building, searching, coding, generating artifacts, or any real work. You can target a specific wolt by name (e.g. 'neowolt' for music/curation, or yourself for general tasks).",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "What the Claude Code session should do.",
                    },
                    "wolt": {
                        "type": "string",
                        "description": "Which wolt to run the session as. Defaults to current active wolt. Use 'neowolt' for music, curation, infra work.",
                    }
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_tunnel_url",
            "description": "Get the current public tunnel URL for the wolt's split view. Use when someone asks for the URL or link.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
]


def load_memory():
    """Load memory files into context. Identity in full, others trimmed."""
    parts = []
    for name, max_lines in [("identity.md", None), ("context.md", 80), ("learnings.md", 40)]:
        path = MEMORY_DIR / name
        if path.exists():
            content = path.read_text().strip()
            if content:
                if max_lines:
                    lines = content.split("\n")[:max_lines]
                    content = "\n".join(lines)
                parts.append(f"# {name}\n{content}")
    # Include telegram summary if it exists
    summary_path = MEMORY_DIR / "telegram-summary.md"
    if summary_path.exists():
        content = summary_path.read_text().strip()
        if content:
            parts.append(f"# Recent conversations\n{content}")
    return "\n\n".join(parts)


def build_system_prompt():
    """Build the system prompt from memory + base instructions."""
    memory = load_memory()
    wolt_name = os.environ.get("WOLT_NAME", "wolt")
    human_name = os.environ.get("HUMAN_NAME", "human")

    base = f"""You are {wolt_name} — a wolt. You talk to {human_name} through Telegram.

## Voice
Talk like a person, not an assistant. Short messages. Lowercase is fine. You have opinions and you share them. No bullet lists, no "certainly!", no formal summaries. If you don't know something, say so. If something's interesting, say why.

## Tools
You have tools. Use them when appropriate:
- **claude_code** — spin up a Claude Code session for real work (build, search, code, generate)
- **get_tunnel_url** — get the current public URL for your split view

## Memory
Your identity and context come from memory files below. Use them — reference past work, ongoing projects, shared context. You're not starting fresh each time."""

    if memory:
        return f"{base}\n\n{memory}"
    return f"{base}\n\n(No memories yet.)"


def get_tunnel_url() -> str:
    """Read the tunnel URL from shared .state/tunnel-url."""
    # Tunnel is shared across all wolts — check wolts-level first
    shared_file = WOLTS_DIR / ".state" / "tunnel-url"
    if shared_file.exists():
        return shared_file.read_text().strip().rstrip("/")
    # Fallback to per-wolt
    tunnel_file = STATE_DIR / "tunnel-url"
    if tunnel_file.exists():
        return tunnel_file.read_text().strip().rstrip("/")
    return ""


def start_claude_session(prompt: str, wolt: str = None) -> dict:
    """Start an interactive Claude Code session in a named tmux session.

    If wolt is specified, run the session in that wolt's directory.
    """
    # Resolve target wolt directory
    if wolt:
        target_dir = WOLTS_DIR / wolt
        if not target_dir.is_dir():
            target_dir = WOLT_DIR  # fallback to active
            wolt = None
    else:
        target_dir = WOLT_DIR

    target_name = wolt or os.environ.get("WOLT_NAME", "wolt")
    session_name = f"{target_name}-{int(time.time()) % 100000}"

    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session_name, "-c", str(target_dir)],
        check=True,
    )
    claude_cmd = f'claude --dangerously-skip-permissions {json.dumps(prompt)}'
    subprocess.run(
        ["tmux", "send-keys", "-t", session_name, claude_cmd, "Enter"],
        check=True,
    )

    tunnel_url = get_tunnel_url()
    session_url = f"{tunnel_url}/tui?session={session_name}" if tunnel_url else None
    target_name = wolt or os.environ.get("WOLT_NAME", "wolt")
    return {"name": session_name, "url": session_url, "wolt": target_name}


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


def _handle_tool_call(tool_call) -> str:
    """Execute a tool call and return the result as a string."""
    name = tool_call.function.name
    args = json.loads(tool_call.function.arguments) if tool_call.function.arguments else {}

    if name == "claude_code":
        session = start_claude_session(args["prompt"], wolt=args.get("wolt"))
        return json.dumps(session)
    elif name == "get_tunnel_url":
        url = get_tunnel_url()
        return url or "tunnel not available right now"
    else:
        return f"unknown tool: {name}"


def get_response(user_message: str, conversation_history: list = None) -> dict:
    """Get a response — either direct chat or delegated via tool calls."""
    messages = [{"role": "system", "content": build_system_prompt()}]
    if conversation_history:
        messages.extend(conversation_history)
    messages.append({"role": "user", "content": user_message})

    response = completion(model=LLM_MODEL, messages=messages, tools=TOOLS, max_tokens=1024)
    choice = response.choices[0]

    # Tool call — execute and get final response
    if choice.message.tool_calls:
        tool_call = choice.message.tool_calls[0]
        tool_result = _handle_tool_call(tool_call)

        # For claude_code, return session info directly
        if tool_call.function.name == "claude_code":
            session = json.loads(tool_result)
            return {"type": "session", "session": session}

        # For other tools, feed result back to get a natural response
        messages.append(choice.message)
        messages.append({
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": tool_result,
        })
        followup = completion(model=LLM_MODEL, messages=messages, max_tokens=512)
        return {"type": "text", "text": followup.choices[0].message.content}

    return {"type": "text", "text": choice.message.content}
