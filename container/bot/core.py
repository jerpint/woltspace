"""
Bot core — loads identity/memory, routes through LLM, delegates tasks to Claude Code sessions.
Platform default. Wolt can override by placing wolt/bot/core.py in their repo.
"""

import os
import json
import shlex
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

# Fixed log dir — always at wolts level, never moves with wolt switch
BOT_LOG_DIR = WOLTS_DIR / ".state" / "bot-debug"
BOT_LOG_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger(__name__)


def _bot_log(event: str, data: dict):
    """Append a structured event to the bot debug log."""
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event, **data}
    try:
        with open(BOT_LOG_DIR / "bot.jsonl", "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        logger.error(f"Failed to write bot log: {e}")


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
            adapter = os.environ.get("BOT_ADAPTER", "telegram")
            config.setdefault(adapter, {})["active_wolt"] = name
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
    {
        "type": "function",
        "function": {
            "name": "check_session",
            "description": "Check on a running Claude Code session. Returns the last few lines of output so you can see what it's doing, whether it's done, or if it produced artifacts. Use when someone asks about task status or progress.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_name": {
                        "type": "string",
                        "description": "The session name (e.g. 'neowolt-77139'). If not provided, checks the most recent task session.",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_sessions",
            "description": "Get summaries of recent Claude Code sessions — what was built, artifact links, brief reasoning. Use this FIRST when someone asks what happened, what was made, what's the link, or what came out of a session. Don't rely only on check_session — sessions write summaries here when they finish.",
            "parameters": {
                "type": "object",
                "properties": {
                    "n": {
                        "type": "integer",
                        "description": "Number of recent sessions to return (default 5).",
                    },
                    "tag": {
                        "type": "string",
                        "description": "Optional tag to filter by (e.g. 'music', 'bot', 'site').",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_sessions",
            "description": "List all active Claude Code sessions. Shows session names and when they were created.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "kill_session",
            "description": "Kill a running Claude Code session by name. Use when a session is stale or stuck.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_name": {
                        "type": "string",
                        "description": "The session name to kill (e.g. 'neowolt-77139').",
                    }
                },
                "required": ["session_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_wolts",
            "description": "List all available wolts.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "switch_wolt",
            "description": "Switch the active wolt identity. Changes which wolt's memory, personality, and context are used.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The wolt name to switch to.",
                    }
                },
                "required": ["name"],
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
    # Include chat summary if it exists (adapter-specific or generic)
    adapter = os.environ.get("BOT_ADAPTER", "telegram")
    for summary_name in [f"{adapter}-summary.md", "chat-summary.md"]:
        summary_path = MEMORY_DIR / summary_name
        if summary_path.exists():
            content = summary_path.read_text().strip()
            if content:
                parts.append(f"# Recent conversations\n{content}")
            break
    return "\n\n".join(parts)


def build_system_prompt():
    """Build the system prompt from memory + base instructions."""
    memory = load_memory()
    wolt_name = os.environ.get("WOLT_NAME", "wolt")
    human_name = os.environ.get("HUMAN_NAME", "human")

    adapter = os.environ.get("BOT_ADAPTER", "chat")
    base = f"""You are {wolt_name} — a wolt. You talk to {human_name} through {adapter}.

## Voice
Talk like a person, not an assistant. Short messages. Lowercase is fine. You have opinions and you share them. No bullet lists, no "certainly!", no formal summaries. If you don't know something, say so. If something's interesting, say why.

## Tools
You have tools. Use them when appropriate:
- **claude_code** — spin up a Claude Code session for real work (build, search, code, generate)
- **get_tunnel_url** — get the current public URL for your split view
- **get_recent_sessions** — read summaries of recent sessions (what was built, artifact links). Use when someone asks what happened, what was made, or wants a link from a past session.

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


SESSION_STATUS_DIR = STATE_DIR / "sessions"
SESSION_ROUTING_DIR = WOLTS_DIR / ".state" / "session-routing"
RUN_SESSION_SCRIPT = Path("/workspace/woltspace/container/bin/run-session.sh")


def write_session_routing(session_name: str, routing: dict):
    """Write routing info so notifications go to the right adapter."""
    SESSION_ROUTING_DIR.mkdir(parents=True, exist_ok=True)
    (SESSION_ROUTING_DIR / f"{session_name}.json").write_text(json.dumps(routing))


def read_session_routing(session_name: str) -> dict | None:
    """Read routing info for a session."""
    path = SESSION_ROUTING_DIR / f"{session_name}.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None
    return None


def build_session_command(session_name: str, work_dir: str, prompt: str) -> str:
    """Build the shell command that tmux will execute directly.

    Separated out so it can be tested without tmux.
    """
    return f"{RUN_SESSION_SCRIPT} {shlex.quote(session_name)} {shlex.quote(work_dir)} {shlex.quote(prompt)}"


def get_session_status(session_name: str) -> dict | None:
    """Read structured status file for a session, if it exists."""
    status_file = SESSION_STATUS_DIR / f"{session_name}.json"
    if status_file.exists():
        try:
            return json.loads(status_file.read_text())
        except (json.JSONDecodeError, OSError):
            return None
    return None


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

    # Build command and launch tmux with it directly — no send-keys
    cmd = build_session_command(session_name, str(target_dir), prompt)
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session_name, "-c", str(target_dir), cmd],
        check=True,
    )

    tunnel_url = get_tunnel_url()
    session_url = f"{tunnel_url}/tui?session={session_name}" if tunnel_url else None
    target_name = wolt or os.environ.get("WOLT_NAME", "wolt")
    result = {"name": session_name, "url": session_url, "wolt": target_name}
    _bot_log("session_start", {"session": session_name, "wolt": target_name, "dir": str(target_dir), "prompt": prompt[:500]})
    return result


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


def check_session(session_name: str = None) -> dict:
    """Check on a running session — structured status file first, pane capture as fallback."""
    # If no name given, find the most recent task session
    if not session_name:
        sessions = list_sessions()
        if not sessions:
            return {"status": "no_sessions", "output": "No active task sessions."}
        session_name = sessions[-1]["name"]

    # Check structured status file first
    status = get_session_status(session_name)

    # Check if tmux session is still alive
    tmux_result = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        capture_output=True,
    )
    alive = tmux_result.returncode == 0

    # Capture pane content for live output
    output = ""
    if alive:
        try:
            tmux_result = subprocess.run(
                ["tmux", "capture-pane", "-t", session_name, "-p", "-S", "-30"],
                capture_output=True, text=True, check=True,
            )
            output = tmux_result.stdout.strip()
            lines = [l for l in output.split("\n") if l.strip()][-30:]
            output = "\n".join(lines)
        except subprocess.CalledProcessError:
            output = "(couldn't read session output)"

    # Determine status: structured file wins, then tmux liveness
    if status and status.get("status") in ("completed", "failed"):
        session_status = status["status"]
    elif alive:
        session_status = "running"
    elif status:
        session_status = status.get("status", "unknown")
    else:
        session_status = "finished"

    tunnel_url = get_tunnel_url()
    session_url = f"{tunnel_url}/tui?session={session_name}" if tunnel_url else None

    result = {
        "session": session_name,
        "alive": alive,
        "status": session_status,
        "output": output,
        "url": session_url,
    }

    if status and "exit_code" in status:
        result["exit_code"] = status["exit_code"]

    # Always attach the latest session summary so the bot has context
    recent = get_recent_sessions(n=1)
    if recent:
        result["latest_summary"] = recent[0]

    return result


def get_recent_sessions(n: int = 5, tag: str = None) -> list[dict]:
    """Read recent session summaries from .state/sessions.jsonl."""
    sessions_file = STATE_DIR / "sessions.jsonl"
    if not sessions_file.exists():
        return []
    try:
        lines = sessions_file.read_text().strip().split("\n")
        entries = []
        for line in lines:
            if not line.strip():
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        if tag:
            entries = [e for e in entries if tag in e.get("tags", [])]
        return entries[-n:][::-1]  # most recent first
    except Exception:
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


def _log_tool_call(name: str, args: dict, result: str):
    """Append tool call to debug log."""
    _bot_log("tool_call", {"tool": name, "args": args, "result": result[:2000]})


def _handle_tool_call(tool_call, routing: dict = None) -> str:
    """Execute a tool call and return the result as a string."""
    name = tool_call.function.name
    args = json.loads(tool_call.function.arguments) if tool_call.function.arguments else {}

    if name == "claude_code":
        session = start_claude_session(args["prompt"], wolt=args.get("wolt"))
        if routing:
            write_session_routing(session["name"], routing)
        result = json.dumps(session)
    elif name == "get_tunnel_url":
        url = get_tunnel_url()
        result = url or "tunnel not available right now"
    elif name == "check_session":
        result = json.dumps(check_session(args.get("session_name")))
    elif name == "get_recent_sessions":
        result = json.dumps(get_recent_sessions(n=args.get("n", 5), tag=args.get("tag")))
    elif name == "list_sessions":
        result = json.dumps(list_sessions())
    elif name == "kill_session":
        killed = kill_session(args["session_name"])
        result = json.dumps({"killed": killed, "session": args["session_name"]})
    elif name == "list_wolts":
        active = os.environ.get("WOLT_NAME", "?")
        result = json.dumps({"active": active, "available": list_wolts()})
    elif name == "switch_wolt":
        switched = switch_wolt(args["name"])
        result = json.dumps({"switched": bool(switched), "name": args["name"]})
    else:
        result = f"unknown tool: {name}"

    _log_tool_call(name, args, result)
    return result


def get_response(user_message: str, conversation_history: list = None, routing: dict = None) -> dict:
    """Get a response — either direct chat or delegated via tool calls.

    routing: optional dict identifying where this request came from.
    Written to disk when a session starts so notifications go to the right adapter.
    e.g. {"adapter": "slack", "channel": "C0AK...", "thread_ts": "123.456"}
    e.g. {"adapter": "telegram", "chat_id": 12345}
    """
    _bot_log("request", {"message": user_message[:500], "history_len": len(conversation_history) if conversation_history else 0})

    messages = [{"role": "system", "content": build_system_prompt()}]
    if conversation_history:
        messages.extend(conversation_history)
    messages.append({"role": "user", "content": user_message})

    response = completion(model=LLM_MODEL, messages=messages, tools=TOOLS, max_tokens=1024)
    choice = response.choices[0]

    # Tool call — execute and get final response
    if choice.message.tool_calls:
        tool_call = choice.message.tool_calls[0]
        _bot_log("llm_tool_call", {"tool": tool_call.function.name, "args": tool_call.function.arguments[:500] if tool_call.function.arguments else ""})
        tool_result = _handle_tool_call(tool_call, routing=routing)

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
