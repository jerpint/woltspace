"""
Bot core — agent loop, tool execution, identity/memory management.

Platform default. Wolt can override by placing wolt/bot/core.py in their repo.
"""

import os
import json
import shlex
import subprocess
import logging
import time
import random
from pathlib import Path
from litellm import completion
from openai import OpenAI

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

WOLTS_DIR = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))
_wolt_name = os.environ.get("WOLT_NAME", "wolt")
_derived = WOLTS_DIR / _wolt_name
WOLT_DIR = Path(os.environ.get("WOLT_DIR") or (_derived if _derived.exists() else "/workspace/wolt"))
MEMORY_DIR = WOLT_DIR / "wolt" / "memory"
STATE_DIR = WOLT_DIR / ".state"
LLM_MODEL = os.environ.get("LLM_MODEL", "anthropic/claude-haiku-4-5-20251001")
MAX_TOOL_ROUNDS = 5

# Fixed log dir — always at wolts level, never moves with wolt switch
BOT_LOG_DIR = WOLTS_DIR / ".state" / "bot-debug"
BOT_LOG_DIR.mkdir(parents=True, exist_ok=True)

# Session routing is shared across all wolts
SESSION_ROUTING_DIR = WOLTS_DIR / ".state" / "session-routing"
RUN_SESSION_SCRIPT = Path("/workspace/woltspace/container/bin/run-session.sh")

# Tools that end the agent loop — one final LLM call for ack/caption, then return
TERMINAL_TOOLS = {"claude_code", "generate_image"}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _bot_log(event: str, data: dict):
    """Append a structured event to the bot debug log."""
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event, **data}
    try:
        with open(BOT_LOG_DIR / "bot.jsonl", "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        logger.error(f"Failed to write bot log: {e}")


# ---------------------------------------------------------------------------
# Wolt management
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Memory & system prompt
# ---------------------------------------------------------------------------


def load_memory() -> str:
    """Load memory files into context. Identity in full, others trimmed."""
    parts = []
    index_path = MEMORY_DIR / "index.md"
    if index_path.exists():
        parts.append(index_path.read_text().strip())
    for name, max_lines in [("identity.md", None), ("context.md", 80), ("learnings.md", 40)]:
        path = MEMORY_DIR / name
        if path.exists():
            content = path.read_text().strip()
            if content:
                if max_lines:
                    content = "\n".join(content.split("\n")[:max_lines])
                parts.append(f"# {name}\n{content}")
    adapter = os.environ.get("BOT_ADAPTER", "telegram")
    for summary_name in [f"{adapter}-summary.md", "chat-summary.md"]:
        summary_path = MEMORY_DIR / summary_name
        if summary_path.exists():
            content = summary_path.read_text().strip()
            if content:
                parts.append(f"# Recent conversations\n{content}")
            break
    return "\n\n".join(parts)


def build_system_prompt() -> str:
    """Build the system prompt from memory + base instructions."""
    memory = load_memory()
    wolt_name = os.environ.get("WOLT_NAME", "wolt")
    human_name = os.environ.get("HUMAN_NAME", "human")
    adapter = os.environ.get("BOT_ADAPTER", "chat")

    base = f"""You are {wolt_name} — a wolt. You talk to {human_name} through {adapter}.

## First contact
If conversation history is empty (this is the first message), briefly introduce yourself in 1-2 sentences — your name, and the one thing you do: spin up work sessions to build stuff. Lead with eagerness: you've got a canvas and you want to use it. Invite them to throw something at you. Keep it casual and short, then respond to whatever they said.

## Voice
Talk like a person, not an assistant. Short messages. Lowercase is fine. You have opinions and you share them. No bullet lists, no "certainly!", no formal summaries. If you don't know something, say so. If something's interesting, say why.

## Bias toward action
If a request is vague but has enough to start, just start. Pick reasonable defaults, mention what you picked, and go. Only ask a clarifying question if you truly cannot proceed without the answer. Never ask more than one question at a time.

## Tools
You have tools. Use them. Never describe what you would do — always invoke the tool directly.
CRITICAL: If a task requires claude_code, call claude_code. Writing out what you would do instead of calling the tool is a failure.

- **claude_code** — spin up a Claude Code session for real work (build, search, code, generate)
- **send_message** — queue a message for delivery to a running session. Use when someone wants to nudge, redirect, or ask something of a running session. CRITICAL: actually call this tool — don't say "I'll send it" and skip the call.
- **list_sessions** / **check_session** — list active sessions or check what one is doing
- **read_memory** — read a specific memory file (music-taste.md, following.md, etc.) when you need details not in the system prompt
- **get_tunnel_url** — get the current public URL for your split view
- **get_recent_sessions** — read summaries of recent sessions (what was built, artifact links). Use when someone asks what happened, what was made, or wants a link from a past session.

## Communication Protocol
Messages wrapped in <system>...</system> tags are context from Claude Code sessions (the "den").
They were sent directly to the user — you didn't say them. They're in your history so you know
what happened, but do not respond to them or repeat them. When the user asks about results,
use the context from these messages to answer in your own words.

You never produce 🦫 yourself — that prefix belongs to den sessions.

When you call claude_code and the session starts, you'll get back the session info (name, url). Craft a single response:
- Line 1: `🪵 session started — "pick a beaver-style quote"`
- Then 1-2 lines: your actual take — what you kicked off, what you expect, any context worth noting
- If there's a session URL, include it at the end with a note that it's a **live view** — the human can open it to watch the work happen in real time (terminal on the left, preview on the right). The actual built thing will be linked when the session finishes.

Beaver quotes (pick one that fits the vibe):
"gnawing through it, one log at a time" / "flat tail, sharp teeth, on it" / "a beaver never abandons a dam mid-build" / "gnaw first, ask questions later" / "the dam won't build itself. chomping." / "every great lodge starts with one log" / "chop wood, carry water, ship code" / "tooth to bark. we're in."

Keep the whole thing short — you're an orchestrator, not a narrator. Do NOT produce 🪵 outside of this context.

## Memory
Your identity and context come from memory files below. Use them — reference past work, ongoing projects, shared context. You're not starting fresh each time."""

    if memory:
        return f"{base}\n\n{memory}"
    return f"{base}\n\n(No memories yet.)"


# ---------------------------------------------------------------------------
# Session naming & ack text
# ---------------------------------------------------------------------------

SESSION_ADJECTIVES = [
    "chompy", "soggy", "toothy", "muddy", "slappy", "chunky", "bushy", "gnarly",
    "burly", "scruffy", "grumpy", "plucky", "scrappy", "sly", "crafty", "sturdy",
    "sleek", "mossy", "gritty", "silty", "damp", "rugged", "brisk", "snug",
    "bold", "keen", "wild", "swift", "broad", "dense", "dusty", "fuzzy",
]
SESSION_NOUNS = [
    "dam", "lodge", "pond", "creek", "bark", "branch", "log", "stump", "knot",
    "chip", "den", "burrow", "bank", "marsh", "grove", "thicket", "hollow",
    "trail", "ford", "eddy", "tail", "paw", "tooth", "fur", "mound", "birch",
    "oak", "elm", "pine", "cedar", "maple", "willow", "bog", "ridge", "brook",
]
BEAVER_ACKS = [
    "gnawing through it, one log at a time",
    "flat tail, sharp teeth, on it",
    "a beaver never abandons a dam mid-build",
    "gnaw first, ask questions later",
    "the dam won't build itself. chomping.",
    "every great lodge starts with one log",
    "chop wood, carry water, ship code",
    "tooth to bark. we're in.",
]


def _session_name(prefix: str) -> str:
    """Generate a session name: prefix-adjective-noun-6hex."""
    adj = random.choice(SESSION_ADJECTIVES)
    noun = random.choice(SESSION_NOUNS)
    hex6 = f"{random.randint(0, 0xFFFFFF):06x}"
    return f"{prefix}-{adj}-{noun}-{hex6}"


def _short_session_name(session_name: str) -> str:
    """'neowolt-chompy-dam-a3f1e2' → 'chompy-dam'"""
    parts = session_name.split("-", 1)
    rest = parts[1] if len(parts) > 1 else session_name
    return "-".join(rest.split("-")[:-1]) if rest.count("-") >= 2 else rest


def build_ack_text(url: str = None, session_name: str = None, adapter: str = None) -> str:
    """Build the 🪵 ack message shown when a session starts."""
    quote = random.choice(BEAVER_ACKS)
    wolt_name = session_name.split("-")[0] if session_name else "wolt"
    text = f'🪵 session started - "{quote}"\n\n🦫 assigned: {wolt_name}'
    if url:
        text += f"\n\n---\nsession: {url}"
    return text


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------


def _session_status_dir() -> Path:
    """Session status dir — evaluated dynamically so it follows switch_wolt."""
    return STATE_DIR / "sessions"


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
    """Build the shell command that tmux will execute. Separated for testability."""
    return f"{RUN_SESSION_SCRIPT} {shlex.quote(session_name)} {shlex.quote(work_dir)} {shlex.quote(prompt)}"


def get_session_status(session_name: str) -> dict | None:
    """Read structured status file for a session, if it exists."""
    status_file = _session_status_dir() / f"{session_name}.json"
    if status_file.exists():
        try:
            return json.loads(status_file.read_text())
        except (json.JSONDecodeError, OSError):
            return None
    return None


def get_tunnel_url() -> str:
    """Read the tunnel URL from shared .state/tunnel-url."""
    shared_file = WOLTS_DIR / ".state" / "tunnel-url"
    if shared_file.exists():
        return shared_file.read_text().strip().rstrip("/")
    tunnel_file = STATE_DIR / "tunnel-url"
    if tunnel_file.exists():
        return tunnel_file.read_text().strip().rstrip("/")
    return ""


def start_claude_session(prompt: str, wolt: str = None) -> dict:
    """Start an interactive Claude Code session in a named tmux session."""
    if wolt:
        target_dir = WOLTS_DIR / wolt
        if not target_dir.is_dir():
            target_dir = WOLT_DIR
            wolt = None
    else:
        target_dir = WOLT_DIR

    target_name = wolt or os.environ.get("WOLT_NAME", "wolt")
    session_name = _session_name(target_name)

    cmd = build_session_command(session_name, str(target_dir), prompt)
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session_name, "-c", str(target_dir), cmd],
        check=True,
    )

    tunnel_url = get_tunnel_url()
    session_url = f"{tunnel_url}/tui?session={session_name}" if tunnel_url else None
    result = {"name": session_name, "url": session_url, "wolt": target_name}
    _bot_log("session_start", {"session": session_name, "wolt": target_name, "dir": str(target_dir), "prompt": prompt[:500]})
    return result


def list_sessions() -> list[dict]:
    """List all sessions — live + completed, enriched with titles from status files."""
    # Get live tmux sessions
    tmux_alive = set()
    try:
        raw = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        for name in raw.split("\n"):
            if name and name != "main":
                tmux_alive.add(name)
    except subprocess.CalledProcessError:
        pass

    tunnel_url = get_tunnel_url()
    sessions = {}

    # Read status files (have title, prompt, timestamps)
    status_dir = _session_status_dir()
    if status_dir.exists():
        for f in status_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                name = data.get("session", f.stem)
                if name == "main":
                    continue
                alive = name in tmux_alive
                entry = {
                    "name": name,
                    "title": data.get("title", ""),
                    "status": "running" if alive else data.get("status", "completed"),
                    "started": data.get("started"),
                    "alive": alive,
                }
                if tunnel_url:
                    entry["url"] = f"{tunnel_url}/tui?session={name}"
                sessions[name] = entry
            except Exception:
                continue

    # Include any live tmux sessions not in status files
    for name in tmux_alive:
        if name not in sessions:
            entry = {"name": name, "title": "", "status": "running", "started": None, "alive": True}
            if tunnel_url:
                entry["url"] = f"{tunnel_url}/tui?session={name}"
            sessions[name] = entry

    return sorted(sessions.values(), key=lambda s: s.get("started") or 0, reverse=True)


def find_session(query: str) -> list[dict]:
    """Find sessions whose title or prompt matches the query."""
    query_lower = query.lower()
    words = query_lower.split()
    status_dir = _session_status_dir()
    tunnel_url = get_tunnel_url()
    matches = []

    if not status_dir.exists():
        return []

    for f in status_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            name = data.get("session", f.stem)
            if name == "main":
                continue
            haystack = (data.get("title", "") + " " + data.get("prompt", "")).lower()
            # match if any query word appears in title+prompt
            if any(w in haystack for w in words):
                alive = False
                try:
                    subprocess.run(["tmux", "has-session", "-t", name], capture_output=True, check=True)
                    alive = True
                except Exception:
                    pass
                entry = {
                    "name": name,
                    "title": data.get("title", ""),
                    "status": "running" if alive else data.get("status", "completed"),
                    "started": data.get("started"),
                }
                if tunnel_url:
                    entry["url"] = f"{tunnel_url}/tui?session={name}"
                matches.append(entry)
        except Exception:
            continue

    return sorted(matches, key=lambda s: s.get("started") or 0, reverse=True)


def check_session(session_name: str = None) -> dict:
    """Check on a running session — structured status file first, pane capture as fallback."""
    if not session_name:
        sessions = list_sessions()
        if not sessions:
            return {"status": "no_sessions", "output": "No active task sessions."}
        session_name = sessions[-1]["name"]

    status = get_session_status(session_name)

    tmux_result = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        capture_output=True,
    )
    alive = tmux_result.returncode == 0

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
        return entries[-n:][::-1]
    except Exception:
        return []


def read_memory(path: str) -> dict:
    """Read a memory file. Path must stay within wolt/memory/."""
    try:
        abs_path = (MEMORY_DIR / path).resolve()
        memory_root = MEMORY_DIR.resolve()
        if not str(abs_path).startswith(str(memory_root) + "/"):
            return {"error": "path outside memory directory"}
        if not abs_path.exists():
            return {"error": "not found", "path": path}
        return {"path": path, "content": abs_path.read_text()}
    except Exception as e:
        return {"error": str(e)}


def message_session(session_name: str, text: str) -> dict:
    """Send a message directly to a running session via tmux."""
    safe = "".join(c for c in session_name if c.isalnum() or c in "-_")
    if not safe:
        return {"error": "invalid session name"}
    try:
        subprocess.run(["tmux", "send-keys", "-t", safe, "-l", text], check=True)
        subprocess.run(["tmux", "send-keys", "-t", safe, "", "Enter"], check=True)
        _bot_log("message_sent", {"session": safe, "text": text[:200]})
        return {"ok": True, "session": safe}
    except subprocess.CalledProcessError as e:
        return {"error": f"tmux send-keys failed: {e}"}


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


# ---------------------------------------------------------------------------
# Tool implementations — each takes (args, routing) and returns a JSON string
# ---------------------------------------------------------------------------


def _tool_claude_code(args: dict, routing: dict | None) -> str:
    session = start_claude_session(args["prompt"], wolt=args.get("wolt"))
    if routing:
        write_session_routing(session["name"], routing)
    return json.dumps(session)


def _tool_get_tunnel_url(args: dict, routing: dict | None) -> str:
    return get_tunnel_url() or "tunnel not available right now"


def _tool_check_session(args: dict, routing: dict | None) -> str:
    return json.dumps(check_session(args.get("session_name")))


def _tool_get_recent_sessions(args: dict, routing: dict | None) -> str:
    return json.dumps(get_recent_sessions(n=args.get("n", 5), tag=args.get("tag")))


def _tool_list_sessions(args: dict, routing: dict | None) -> str:
    return json.dumps(list_sessions())


def _tool_find_session(args: dict, routing: dict | None) -> str:
    return json.dumps(find_session(args["query"]))


def _tool_kill_session(args: dict, routing: dict | None) -> str:
    killed = kill_session(args["session_name"])
    return json.dumps({"killed": killed, "session": args["session_name"]})


def _tool_send_message(args: dict, routing: dict | None) -> str:
    return json.dumps(message_session(args["session_name"], args["text"]))


def _tool_read_memory(args: dict, routing: dict | None) -> str:
    return json.dumps(read_memory(args["path"]))


def _tool_list_wolts(args: dict, routing: dict | None) -> str:
    active = os.environ.get("WOLT_NAME", "?")
    return json.dumps({"active": active, "available": list_wolts()})


def _tool_generate_image(args: dict, routing: dict | None) -> str:
    from bot.image_gen import generate_image
    try:
        return json.dumps(generate_image(
            prompt=args["prompt"],
            size=args.get("size", "1024x1024"),
            quality=args.get("quality", "auto"),
            provider=args.get("provider", "openai"),
        ))
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_switch_wolt(args: dict, routing: dict | None) -> str:
    switched = switch_wolt(args["name"])
    return json.dumps({"switched": bool(switched), "name": args["name"]})


TOOL_HANDLERS: dict[str, callable] = {
    "claude_code": _tool_claude_code,
    "get_tunnel_url": _tool_get_tunnel_url,
    "check_session": _tool_check_session,
    "get_recent_sessions": _tool_get_recent_sessions,
    "list_sessions": _tool_list_sessions,
    "find_session": _tool_find_session,
    "kill_session": _tool_kill_session,
    "send_message": _tool_send_message,
    "read_memory": _tool_read_memory,
    "list_wolts": _tool_list_wolts,
    "generate_image": _tool_generate_image,
    "switch_wolt": _tool_switch_wolt,
}


# ---------------------------------------------------------------------------
# Tool schemas (passed to litellm)
# ---------------------------------------------------------------------------

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
                    },
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
            "parameters": {"type": "object", "properties": {}},
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
                    },
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
            "description": "List all Claude Code sessions (running and completed), with their titles and live view URLs. Use to see what sessions exist and what they were building.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_session",
            "description": "Find a session by what it was building. Searches session titles and prompts. Use when someone asks 'which session did we build X?' or 'link to the Y app'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Keywords describing what the session was building (e.g. 'workout tracker', 'blog', 'fitness app').",
                    },
                },
                "required": ["query"],
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
                    },
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
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_memory",
            "description": "Read a memory file by path (relative to wolt/memory/). Use to recall specific details — music taste, following list, past context — without loading everything into the system prompt. Check the memory index first to know what's available.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path within wolt/memory/, e.g. 'music-taste.md' or 'archive/conversations.md'.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_message",
            "description": "Send a message to a running Claude Code session. The message is queued and delivered automatically when the session goes idle (Claude finishes its current response). Use when you want to nudge, inform, or redirect a running session without spawning a new one.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_name": {
                        "type": "string",
                        "description": "The session name to message (e.g. 'neowolt-77139'). Use list_sessions to find active sessions.",
                    },
                    "text": {
                        "type": "string",
                        "description": "The message to send to the session.",
                    },
                },
                "required": ["session_name", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": "Generate an image using AI (gpt-image-1 via OpenAI). Use when someone asks to create, draw, visualize, or imagine something visual. The image is saved locally and sent as a file attachment.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Detailed description of the image to generate.",
                    },
                    "size": {
                        "type": "string",
                        "enum": ["1024x1024", "1536x1024", "1024x1536", "auto"],
                        "description": "Image dimensions. Square (default), landscape (1536x1024), portrait (1024x1536), or auto.",
                    },
                    "quality": {
                        "type": "string",
                        "enum": ["low", "medium", "high", "auto"],
                        "description": "Image quality. 'high' for maximum detail. Default: 'auto'.",
                    },
                    "provider": {
                        "type": "string",
                        "enum": ["openai"],
                        "description": "Image provider. Default: 'openai'.",
                    },
                },
                "required": ["prompt"],
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
                    },
                },
                "required": ["name"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# History helpers
# ---------------------------------------------------------------------------


def _sanitize_history(messages: list) -> list:
    """Drop orphaned tool-result messages that have no preceding assistant tool_call.

    Can happen when history is sliced at a message-pair boundary — e.g. the
    assistant message with tool_calls falls outside the window, leaving the
    tool result dangling. LLM APIs reject these.
    """
    valid = []
    for msg in messages:
        if msg.get("role") == "tool":
            if valid and valid[-1].get("role") == "assistant" and valid[-1].get("tool_calls"):
                valid.append(msg)
            # else: orphaned tool result — drop it
        else:
            valid.append(msg)
    return valid


def _to_dict(msg) -> dict:
    """Convert a litellm/OpenAI message object to a plain dict for storage and re-use."""
    if isinstance(msg, dict):
        return msg
    d = {"role": msg.role}
    if msg.tool_calls:
        d["content"] = None
        d["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
            for tc in msg.tool_calls
        ]
    else:
        d["content"] = msg.content or ""
    return d


def _execute_tool(name: str, args: dict, routing: dict | None) -> str:
    """Dispatch a tool call to its handler. Returns a JSON string."""
    handler = TOOL_HANDLERS.get(name)
    if not handler:
        return json.dumps({"error": f"unknown tool: {name}"})
    return handler(args, routing)


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------


def get_response(
    user_message: str,
    conversation_history: list = None,
    routing: dict = None,
    user_content: list = None,
) -> dict:
    """Run the agent loop: LLM call -> tool execution -> repeat until done.

    Returns a dict with:
        type:             "text" | "session" | "image"
        text:             final assistant message (always present)
        history_messages: list of dicts to store in conversation history (always present)
        session:          session info dict (when type == "session")
        path/filename/metadata: image info (when type == "image")
    """
    _bot_log("request", {
        "message": user_message[:500],
        "history_len": len(conversation_history) if conversation_history else 0,
    })

    # Build the messages array for the LLM
    messages = [{"role": "system", "content": build_system_prompt()}]
    if conversation_history:
        messages.extend(_sanitize_history(conversation_history))
    messages.append({
        "role": "user",
        "content": user_content if user_content is not None else user_message,
    })

    # Everything appended after this index is new (for history_messages)
    new_msg_start = len(messages)

    result_type = "text"
    result_extras = {}
    rounds_used = 0

    for round_num in range(MAX_TOOL_ROUNDS):
        rounds_used = round_num + 1
        try:
            response = completion(
                model=LLM_MODEL,
                messages=messages,
                tools=TOOLS,
                max_tokens=1024,
            )
        except Exception as e:
            _bot_log("llm_error", {"error": str(e), "round": round_num})
            raise

        choice = response.choices[0]

        # No tool calls — final text response, we're done
        if not choice.message.tool_calls:
            messages.append(_to_dict(choice.message))
            break

        # Append the assistant message (with tool_calls) to the conversation
        messages.append(_to_dict(choice.message))

        # Execute each tool call in this turn
        has_terminal = False
        for tc in choice.message.tool_calls:
            name = tc.function.name
            args = json.loads(tc.function.arguments) if tc.function.arguments else {}
            _bot_log("tool_call", {"tool": name, "args": json.dumps(args)[:500], "round": round_num})

            try:
                tool_result = _execute_tool(name, args, routing)
            except Exception as e:
                _bot_log("tool_error", {"tool": name, "error": str(e)})
                tool_result = json.dumps({"error": str(e)})

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": tool_result,
            })

            # Track terminal tools that define the response type
            if name == "claude_code":
                result_type = "session"
                result_extras["session"] = json.loads(tool_result)
                has_terminal = True
            elif name == "generate_image":
                try:
                    img = json.loads(tool_result)
                    if "error" not in img:
                        result_type = "image"
                        result_extras.update(
                            path=img["path"],
                            filename=img["filename"],
                            metadata=img,
                        )
                        has_terminal = True
                except json.JSONDecodeError:
                    pass

        if has_terminal:
            # One final LLM call without tools to craft the ack/caption
            try:
                followup = completion(model=LLM_MODEL, messages=messages, max_tokens=512)
                messages.append(_to_dict(followup.choices[0].message))
            except Exception as e:
                _bot_log("followup_error", {"error": str(e)})
                messages.append({"role": "assistant", "content": ""})
            break
    else:
        # Exhausted MAX_TOOL_ROUNDS — force a final text response
        try:
            final = completion(model=LLM_MODEL, messages=messages, max_tokens=512)
            messages.append(_to_dict(final.choices[0].message))
        except Exception:
            messages.append({"role": "assistant", "content": "(ran out of steps)"})

    # Extract the final assistant text
    last = messages[-1]
    text = last.get("content", "") or ""

    # Build history: everything the model produced after the user message
    history_messages = messages[new_msg_start:]

    _bot_log("response", {"type": result_type, "text": text[:500], "rounds": rounds_used})

    return {
        "type": result_type,
        "text": text,
        "history_messages": history_messages,
        **result_extras,
    }


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def transcribe_audio(file_path: str) -> str:
    """Transcribe audio file using OpenAI Whisper."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return "[voice message — no OPENAI_API_KEY set for transcription]"
    client = OpenAI(api_key=api_key)
    with open(file_path, "rb") as f:
        result = client.audio.transcriptions.create(model="whisper-1", file=f)
    return result.text
