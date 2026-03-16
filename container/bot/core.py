"""
Bot core — agent loop, tool execution, identity/memory management.

Platform default. Wolt can override by placing wolt/bot/core.py in their repo.
"""

import os
import json
import shlex
import subprocess
import logging
import sys
import time
import random
from pathlib import Path
from litellm import completion
from openai import OpenAI

# Add lib/ to path for sessions module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from sessions import SessionRegistry
from wolts import get_active_creature, find_by_type

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

WOLTS_DIR = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))
WOLTSPACE_DIR = Path(os.environ.get("WOLTSPACE_DIR", "/workspace/woltspace"))
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

# Session registry — single source of truth for all session metadata
registry = SessionRegistry(WOLTS_DIR / ".state" / "registry")

RUN_SESSION_SCRIPT = Path("/workspace/woltspace/container/bin/run-session.sh")

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


def _load_dog_identity() -> str | None:
    """Load identity from the active dog-wolt, if one exists."""
    dog_name = get_active_creature("dog")
    if not dog_name:
        return None
    dog_dir = WOLTS_DIR / dog_name / "wolt" / "memory" / "identity.md"
    if dog_dir.exists():
        return dog_dir.read_text().strip()
    return None


def _load_prompt(name: str, variables: dict = None) -> str:
    """Load a prompt fragment from the dog species prompts/ directory.

    Supports {variable} substitution for dynamic values.
    Returns empty string if file not found.
    """
    prompt_path = WOLTSPACE_DIR / "species" / "dog" / "prompts" / f"{name}.md"
    if not prompt_path.exists():
        return ""
    text = prompt_path.read_text().strip()
    if variables:
        try:
            text = text.format(**variables)
        except KeyError:
            pass  # leave unresolved placeholders as-is
    return text


def _build_creatures_list() -> dict:
    """Build creature_list and species_list strings from species definitions."""
    try:
        from species import list_species
        all_species = list_species()
        if not all_species:
            return {"creature_list": "", "species_list": ""}

        # Rodent tiers
        rodent = all_species.get("rodent", {})
        tiers = rodent.get("tiers", {})
        tier_parts = []
        for name, tier in tiers.items():
            emoji = tier.get("emoji", "")
            desc = tier.get("description", "")
            model = tier.get("model", "")
            for alias in ("opus", "sonnet", "haiku"):
                if alias in model:
                    model = alias
                    break
            tier_parts.append(f"{emoji} **{name}** ({model} — {desc})")

        # Other species
        species_lines = []
        for name, sp in all_species.items():
            if name in ("rodent", "dog"):
                continue
            emoji = sp.get("emoji", "")
            desc = sp.get("description", "")
            species_lines.append(f"{emoji} **{name}** — {desc}")

        return {
            "creature_list": ", ".join(tier_parts),
            "species_list": "\n".join(species_lines),
        }
    except (ImportError, Exception):
        return {
            "creature_list": "🦝 **raccoon** (opus — complex reasoning), 🦫 **beaver** (sonnet — building, coding), 🦦 **otter** (haiku — fast, lightweight)",
            "species_list": "🐺 **wolf** — cron & scheduler\n🕷️ **spider** — headless browser\n🐻 **bear** — safety & validation\n🐼 **panda** — daily reminders",
        }


def build_system_prompt() -> str:
    """Build the system prompt by loading and composing text files.

    Prompt fragments live in species/dog/prompts/. Each section is a standalone
    .md file. This function just loads, substitutes variables, and concatenates.
    """
    memory = load_memory()
    wolt_name = os.environ.get("WOLT_NAME", "wolt")
    human_name = os.environ.get("HUMAN_NAME", "human")
    adapter = os.environ.get("BOT_ADAPTER", "chat")

    # Shared variables for template substitution
    dog_identity = _load_dog_identity()
    dog_name = get_active_creature("dog") or wolt_name
    creatures = _build_creatures_list()

    variables = {
        "wolt_name": wolt_name,
        "human_name": human_name,
        "adapter": adapter,
        "dog_name": dog_name,
        **creatures,
    }

    # Build intro — use dog identity if available, otherwise fallback
    if dog_identity:
        intro = _load_prompt("intro", variables)
        if intro:
            intro += f"\n\n## Your identity\n{dog_identity}"
        else:
            intro = f"You are {dog_name} — the dog. You talk to {human_name} through {adapter}.\n\n## Your identity\n{dog_identity}"
    else:
        intro = _load_prompt("intro-fallback", variables)
        if not intro:
            intro = f"You are {wolt_name} — a wolt (the dog). You talk to {human_name} through {adapter}."

    # Load each prompt section — order matters
    sections = [
        intro,
        _load_prompt("creatures", variables),
        _load_prompt("voice", variables),
        _load_prompt("tools", variables),
        _load_prompt("projects", variables),
        _load_prompt("communication", variables),
        _load_prompt("memory", variables),
    ]

    # Filter empty sections, join with double newlines
    base = "\n\n".join(s for s in sections if s)

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


CREATURE_EMOJIS = {
    "raccoon": "🦝",
    "beaver": "🦫",
    "otter": "🦦",
    # Planned creatures — not yet active as session types
    "dog": "🐶",
    "wolf": "🐺",
    "spider": "🕷️",
    "bear": "🐻",
    "panda": "🐼",
}


def build_ack_text(url: str = None, session_name: str = None, adapter: str = None, creature: str = None) -> str:
    """Build the 🪵 ack message shown when a session starts."""
    quote = random.choice(BEAVER_ACKS)
    wolt_name = session_name.split("-")[0] if session_name else "wolt"
    emoji = CREATURE_EMOJIS.get(creature, "🦫")
    text = f'🪵 session started - "{quote}"\n\n{emoji} assigned: {wolt_name}'
    if url:
        text += f"\n\n---\nsession: {url}"
    return text


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------


def build_session_command(session_name: str, work_dir: str, prompt: str, model: str = None) -> str:
    """Build the shell command that tmux will execute. Separated for testability."""
    cmd = f"{RUN_SESSION_SCRIPT} {shlex.quote(session_name)} {shlex.quote(work_dir)} {shlex.quote(prompt)}"
    if model:
        cmd += f" {shlex.quote(model)}"
    return cmd


def get_session_status(session_name: str) -> dict | None:
    """Read session status from registry."""
    return registry.get(session_name, check_alive=False)


def get_tunnel_url() -> str:
    """Read the tunnel URL from shared .state/tunnel-url."""
    shared_file = WOLTS_DIR / ".state" / "tunnel-url"
    if shared_file.exists():
        return shared_file.read_text().strip().rstrip("/")
    tunnel_file = STATE_DIR / "tunnel-url"
    if tunnel_file.exists():
        return tunnel_file.read_text().strip().rstrip("/")
    return ""


def _call_server(method: str, path: str, body: dict | None = None) -> dict:
    """Make an HTTP request to the local woltspace server."""
    import urllib.request
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://localhost:3000{path}",
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


# Creature model resolution — reads from species/ definitions, falls back to hardcoded
def _resolve_creature_model(creature: str) -> str | None:
    """Resolve a creature name to a model alias using species definitions."""
    try:
        from species import get_creature_model
        model = get_creature_model(creature)
        if model:
            # Convert full model ID to alias (anthropic/claude-sonnet-4 → sonnet)
            for alias in ("opus", "sonnet", "haiku"):
                if alias in model:
                    return alias
            return model
    except ImportError:
        pass
    # Fallback: hardcoded mapping (removed once species/ is stable)
    return {
        "raccoon": "opus",
        "beaver": "sonnet",
        "otter": "haiku",
        "wolf": "sonnet",
    }.get(creature)


def start_claude_session(prompt: str, wolt: str = None, creature: str = None, routing: dict = None, project: str = None) -> dict:
    """Start an interactive Claude Code session in a named tmux session.

    creature: optional "raccoon" (opus) or "beaver" (sonnet) to pick the model.
    routing: adapter routing info (adapter, chat_id, etc.) — written to registry.
    project: optional project name — session will run in wolt/projects/{project}/.
    """
    if wolt:
        target_dir = WOLTS_DIR / wolt
        if not target_dir.is_dir():
            target_dir = WOLT_DIR
            wolt = None
    else:
        target_dir = WOLT_DIR

    # If a project is specified, scope the session to that project directory
    if project:
        project_dir = target_dir / "wolt" / "projects" / project
        project_dir.mkdir(parents=True, exist_ok=True)
        target_dir = project_dir

    target_name = wolt or os.environ.get("WOLT_NAME", "wolt")
    session_name = _session_name(target_name)
    model = _resolve_creature_model(creature) if creature else None

    tunnel_url = get_tunnel_url()
    session_url = f"{tunnel_url}/tui?session={session_name}" if tunnel_url else ""

    # Register the session (single source of truth)
    registry.create(
        session_name,
        wolt=target_name,
        creature=creature or "",
        model=model or "",
        dir=str(target_dir),
        project=project or "",
        prompt=prompt,
        adapter=(routing or {}).get("adapter", ""),
        chat_id=str((routing or {}).get("chat_id", "")),
        user_id=str((routing or {}).get("user_id", "")),
        thread_ts=str((routing or {}).get("thread_ts", "")),
        session_url=session_url,
    )

    cmd = build_session_command(session_name, str(target_dir), prompt, model=model)
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session_name, "-c", str(target_dir), cmd],
        check=True,
    )

    result = {"name": session_name, "url": session_url or None, "wolt": target_name}
    if project:
        result["project"] = project
    if creature:
        result["creature"] = creature
        result["model"] = model
    _bot_log("session_start", {"session": session_name, "wolt": target_name, "project": project, "dir": str(target_dir), "creature": creature, "model": model, "prompt": prompt[:500]})
    return result


def new_session(prompt: str, from_session: str = None, wolt: str = None, creature: str = None, routing: dict = None, project: str = None) -> dict:
    """Start a fresh Claude Code session and redirect the current viewport to it."""
    session = start_claude_session(prompt, wolt=wolt, creature=creature, routing=routing, project=project)

    # Determine which viewport to redirect
    redirect_from = from_session
    if not redirect_from:
        sessions = list_sessions()
        alive = [s for s in sessions if s.get("alive") and s["name"] != session["name"]]
        if alive:
            redirect_from = alive[0]["name"]

    if redirect_from:
        try:
            _call_server("POST", "/sessions/redirect", {"from": redirect_from, "to": session["name"]})
            session["redirected_from"] = redirect_from
        except Exception as e:
            session["redirect_error"] = str(e)

    return session


def list_sessions() -> list[dict]:
    """List all sessions from the registry, enriched with liveness from tmux."""
    tunnel_url = get_tunnel_url()
    sessions = registry.list()
    # Normalize output format for compatibility
    results = []
    for s in sessions:
        entry = {
            "name": s["name"],
            "title": s.get("title", ""),
            "status": s.get("status", "unknown"),
            "started": s.get("created_at"),
            "alive": s.get("alive", False),
        }
        if tunnel_url:
            entry["url"] = f"{tunnel_url}/tui?session={s['name']}"
        results.append(entry)
    return results


def find_session(query: str) -> list[dict]:
    """Find sessions whose title or prompt matches the query."""
    query_lower = query.lower()
    words = query_lower.split()
    tunnel_url = get_tunnel_url()
    matches = []

    for s in registry.list():
        haystack = (s.get("title", "") + " " + s.get("prompt", "")).lower()
        if any(w in haystack for w in words):
            entry = {
                "name": s["name"],
                "title": s.get("title", ""),
                "status": s.get("status", "unknown"),
                "started": s.get("created_at"),
            }
            if tunnel_url:
                entry["url"] = f"{tunnel_url}/tui?session={s['name']}"
            matches.append(entry)

    return matches


def check_session(session_name: str = None) -> dict:
    """Check on a running session — registry + pane capture."""
    if not session_name:
        sessions = list_sessions()
        if not sessions:
            return {"status": "no_sessions", "output": "No active task sessions."}
        session_name = sessions[-1]["name"]

    data = registry.get(session_name, check_alive=True)
    alive = data["alive"] if data else False

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

    if data:
        session_status = data["status"]
    elif alive:
        session_status = "running"
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
    if data and data.get("exit_code") is not None:
        result["exit_code"] = data["exit_code"]

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


def _session_has_claude(session_name: str) -> bool | None:
    """Check if a tmux session has a claude process running in its pane.

    Returns True if claude is in the process tree, False if only a shell is running,
    None if the session doesn't exist.

    Uses pane_pid to walk the process tree instead of pane_current_command,
    which only shows the foreground process (unreliable when Claude runs subprocesses).
    """
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-t", session_name, "-F", "#{pane_pid}"],
            capture_output=True, text=True, check=True,
        )
        pane_pid = result.stdout.strip()
        if not pane_pid:
            return None
        # Check all descendants of the pane's shell for a claude process
        ps_result = subprocess.run(
            ["ps", "--ppid", pane_pid, "-o", "comm=", "--no-headers"],
            capture_output=True, text=True,
        )
        children = ps_result.stdout.strip().split("\n")
        # claude or run-session (still launching) means alive
        for child in children:
            child = child.strip()
            if child in ("claude", "run-session.sh", "run-session"):
                return True
        return False
    except subprocess.CalledProcessError:
        return None


def message_session(session_name: str, text: str) -> dict:
    """Send a message to a session. If Claude has exited, restart it with --continue."""
    safe = "".join(c for c in session_name if c.isalnum() or c in "-_")
    if not safe:
        return {"error": "invalid session name"}

    tunnel_url = get_tunnel_url()
    session_url = f"{tunnel_url}/tui?session={safe}" if tunnel_url else None

    # Check if tmux session exists and whether Claude is running
    claude_alive = _session_has_claude(safe)
    if claude_alive is None:
        return {"ok": False, "error": f"session {safe} not found — it may have been killed or expired", "session": safe, "url": session_url}

    # If Claude is still running, send keys directly
    if claude_alive:
        try:
            subprocess.run(["tmux", "send-keys", "-t", safe, "-l", text], check=True)
            subprocess.run(["tmux", "send-keys", "-t", safe, "", "Enter"], check=True)
            _bot_log("message_sent", {"session": safe, "text": text[:200]})
            return {"ok": True, "session": safe, "url": session_url, "status": "delivered", "detail": "Claude is running, message sent directly"}
        except subprocess.CalledProcessError as e:
            return {"ok": False, "error": f"tmux send-keys failed: {e}", "session": safe, "url": session_url}

    # Claude exited — restart with --resume (specific session ID) or --continue (fallback)
    _bot_log("session_revive", {"session": safe, "text": text[:200]})
    try:
        # Try to get the Claude session UUID from the registry so we resume the right conversation
        reg_data = registry.get(safe, check_alive=False)
        claude_session_id = reg_data.get("claude_session_id") if reg_data else None
        if claude_session_id:
            resume_flag = f"--resume {shlex.quote(claude_session_id)}"
        else:
            # Fallback for sessions started before this fix
            resume_flag = "--continue"
        _bot_log("session_revive_method", {"session": safe, "claude_session_id": claude_session_id or "none", "method": "resume" if claude_session_id else "continue"})
        resume_cmd = f"export WOLT_SESSION={shlex.quote(safe)} && claude --dangerously-skip-permissions {resume_flag} {shlex.quote(text)}"
        subprocess.run(["tmux", "send-keys", "-t", safe, "-l", resume_cmd], check=True)
        subprocess.run(["tmux", "send-keys", "-t", safe, "", "Enter"], check=True)
        return {"ok": True, "session": safe, "url": session_url, "status": "revived", "detail": f"Claude had exited — restarted with {'--resume ' + claude_session_id if claude_session_id else '--continue'} and delivered message"}
    except subprocess.CalledProcessError as e:
        return {"ok": False, "error": f"session revive failed: {e}", "session": safe, "url": session_url}


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
    creature = args.get("creature")
    session = start_claude_session(args["prompt"], wolt=args.get("wolt"), creature=creature, routing=routing, project=args.get("project"))
    return json.dumps(session)


def _tool_new_session(args: dict, routing: dict | None) -> str:
    creature = args.get("creature")
    session = new_session(
        args["prompt"],
        from_session=args.get("from_session"),
        wolt=args.get("wolt"),
        creature=creature,
        routing=routing,
        project=args.get("project"),
    )
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
    name = args["session_name"]
    killed = kill_session(name)
    tunnel_url = get_tunnel_url()
    url = f"{tunnel_url}/tui?session={name}" if tunnel_url else None
    if killed:
        return json.dumps({"ok": True, "session": name, "url": url, "detail": f"session {name} killed"})
    return json.dumps({"ok": False, "session": name, "url": url, "error": f"couldn't kill {name} — may not exist or is the main session"})


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


def _tool_list_projects(args: dict, routing: dict | None) -> str:
    projects_dir = WOLT_DIR / "wolt" / "projects"
    projects = []
    if projects_dir.exists():
        for entry in sorted(projects_dir.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            proj = {"name": entry.name, "path": str(entry)}
            proj_json = entry / "project.json"
            if proj_json.exists():
                try:
                    config = json.loads(proj_json.read_text())
                    proj.update(config)
                except Exception:
                    pass
            projects.append(proj)
    return json.dumps({"projects": projects, "count": len(projects)})


def _tool_switch_wolt(args: dict, routing: dict | None) -> str:
    switched = switch_wolt(args["name"])
    return json.dumps({"switched": bool(switched), "name": args["name"]})


def _tool_check_update(args: dict, routing: dict | None) -> str:
    """Check if a woltspace update is available."""
    import subprocess
    version_file = WOLT_DIR / ".state" / "woltspace-version"
    branch_file = WOLT_DIR / ".state" / "woltspace-branch"
    # Check against the branch we built from (default: main)
    build_branch = branch_file.read_text().strip() if branch_file.exists() else "main"
    try:
        result = subprocess.run(
            ["git", "ls-remote", "https://github.com/jerpint/woltspace.git", f"refs/heads/{build_branch}"],
            capture_output=True, text=True, timeout=10,
        )
        remote_head = result.stdout.strip().split("\t")[0] if result.stdout.strip() else ""
        if not remote_head:
            return json.dumps({"error": "could not reach remote"})

        local_version = version_file.read_text().strip() if version_file.exists() else ""

        if not local_version:
            # First check — store version for future comparisons
            version_file.parent.mkdir(parents=True, exist_ok=True)
            version_file.write_text(remote_head)
            return json.dumps({"up_to_date": True, "version": remote_head[:7], "note": "version tracking initialized"})

        if local_version == remote_head:
            return json.dumps({"up_to_date": True, "version": remote_head[:7]})

        return json.dumps({
            "up_to_date": False,
            "local": local_version[:7],
            "remote": remote_head[:7],
            "message": "an update is available — route to beaver or raccoon to handle it. NEVER use otter for updates.",
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


def _get_wolf_wolt_dir() -> Path:
    """Resolve the wolf's working directory — same logic as creatures/wolf.py.

    Checks woltspace.json for active_wolf first, falls back to WOLT_DIR.
    """
    config_file = WOLTS_DIR / "woltspace.json"
    if config_file.exists():
        try:
            config = json.loads(config_file.read_text())
            active_wolf = config.get("creatures", {}).get("active_wolf")
            if active_wolf:
                wolf_dir = WOLTS_DIR / active_wolf
                if (wolf_dir / "wolt" / "wolf.json").exists():
                    return wolf_dir
        except (json.JSONDecodeError, OSError):
            pass
    return WOLT_DIR


def _tool_wolf_schedules(args: dict, routing: dict | None) -> str:
    """List all wolf cron schedules for the active wolt."""
    wolf_wolt = _get_wolf_wolt_dir()
    wolf_config = wolf_wolt / "wolt" / "wolf.json"
    if not wolf_config.exists():
        return json.dumps({"crons": [], "count": 0, "note": "no wolf.json — no crons configured"})
    try:
        data = json.loads(wolf_config.read_text())
        crons = data.get("crons", [])
        # Enrich with last-run info
        state_dir = wolf_wolt / ".state" / "wolf"
        for entry in crons:
            name = entry.get("name", "")
            last_file = state_dir / f"{name}.last"
            entry["last_run"] = last_file.read_text().strip() if last_file.exists() else "never"
        return json.dumps({"crons": crons, "count": len(crons)})
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_fire_wolf(args: dict, routing: dict | None) -> str:
    """Fire a specific wolf cron by name, ignoring its schedule."""
    name = args.get("name", "").strip()
    if not name:
        return json.dumps({"error": "name is required"})

    # Import and call fire_by_name from wolf module
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from creatures.wolf import fire_by_name
        fired = fire_by_name(name)
        if fired:
            return json.dumps({"ok": True, "fired": name})
        else:
            return json.dumps({"ok": False, "error": f"cron '{name}' not found in wolf.json"})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})


def _tool_wolf_jobs(args: dict, routing: dict | None) -> str:
    """Show recent wolf job log entries."""
    count = args.get("count", 10)
    wolf_wolt = _get_wolf_wolt_dir()
    log_file = wolf_wolt / ".state" / "wolf" / "jobs.jsonl"
    if not log_file.exists():
        return json.dumps({"jobs": [], "note": "no job log yet"})
    try:
        lines = log_file.read_text().strip().split("\n")
        recent = lines[-count:]
        jobs = []
        for line in recent:
            try:
                jobs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return json.dumps({"jobs": jobs, "count": len(jobs)})
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_open_issue(args: dict, routing: dict | None) -> str:
    """Open a GitHub issue on the woltspace repo."""
    import subprocess
    title = args.get("title", "").strip()
    body = args.get("body", "").strip()
    labels = args.get("labels", [])
    if not title:
        return json.dumps({"error": "title is required"})

    # Read PAT from wolt .env
    env_file = WOLT_DIR / ".env"
    gh_token = None
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("GH_PAT_TOKEN="):
                gh_token = line.split("=", 1)[1].strip()
                break

    # Fall back to env var
    if not gh_token:
        gh_token = os.environ.get("GH_PAT_TOKEN", "")

    if not gh_token:
        return json.dumps({"error": "GH_PAT_TOKEN not found — add it to .env"})

    # Tag the issue with which wolt opened it — header, natural tone
    header = f"🐶 *{_wolt_name}* noticed this one.\n\n"
    full_body = header + body if body else header.strip()

    cmd = ["gh", "issue", "create", "--repo", "jerpint/woltspace", "--title", title]
    cmd += ["--body", full_body]
    if labels:
        cmd += ["--label", ",".join(labels)]

    env = {**os.environ, "GH_TOKEN": gh_token}
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, env=env)
        if result.returncode == 0:
            url = result.stdout.strip()
            return json.dumps({"url": url, "title": title})
        return json.dumps({"error": result.stderr.strip() or "gh issue create failed"})
    except Exception as e:
        return json.dumps({"error": str(e)})


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
    "list_projects": _tool_list_projects,
    "switch_wolt": _tool_switch_wolt,
    "new_session": _tool_new_session,
    "wolf_schedules": _tool_wolf_schedules,
    "fire_wolf": _tool_fire_wolf,
    "wolf_jobs": _tool_wolf_jobs,
    "check_update": _tool_check_update,
    "open_issue": _tool_open_issue,
}


# ---------------------------------------------------------------------------
# Tool schemas (passed to litellm)
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "claude_code",
            "description": "Delegate a task to a Claude Code session. Use for building, searching, coding, generating artifacts, or any real work. You can target a specific wolt, pick a creature type, and scope to a project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "What the Claude Code session should do.",
                    },
                    "wolt": {
                        "type": "string",
                        "description": "Which wolt to run the session as. Defaults to current active wolt.",
                    },
                    "creature": {
                        "type": "string",
                        "enum": ["raccoon", "beaver", "otter", "wolf"],
                        "description": "Which creature to run: 'raccoon' (🦝 opus — complex reasoning), 'beaver' (🦫 sonnet — building, coding), 'otter' (🦦 haiku — fast tasks), or 'wolf' (🐺 sonnet — cron management). Defaults to the wolt's default model if omitted.",
                    },
                    "project": {
                        "type": "string",
                        "description": "Project name to scope the session to. Session will run in wolt/projects/{name}/. The directory is created if it doesn't exist. Use this to keep work isolated from the wolt root.",
                    },
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "new_session",
            "description": "Start a fresh Claude Code session and switch the current viewport to it. Prefer over claude_code when the intent is starting an interactive session, not delegating a background task.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Opening message for the new session. Use 'hey nw' for a standard greeting, or a specific task prompt.",
                    },
                    "from_session": {
                        "type": "string",
                        "description": "Session name currently shown in the viewport to redirect. If omitted, redirects the most recently active session.",
                    },
                    "wolt": {
                        "type": "string",
                        "description": "Which wolt to run the session as. Defaults to current active wolt.",
                    },
                    "creature": {
                        "type": "string",
                        "enum": ["raccoon", "beaver", "otter", "wolf"],
                        "description": "Which creature to run: 'raccoon' (opus), 'beaver' (sonnet), 'otter' (haiku — fast tasks), or 'wolf' (sonnet — cron management). Defaults to wolt's default model.",
                    },
                    "project": {
                        "type": "string",
                        "description": "Project name to scope the session to. Session runs in wolt/projects/{name}/.",
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
            "description": "Send a message to a Claude Code session. If Claude is still running, delivers directly. If Claude exited (session idle at shell), revives it with --continue and delivers the message. Check the result: 'revived: true' means the session had died and was restarted — tell the user.",
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
            "name": "list_projects",
            "description": "List all projects in the current wolt. Returns project names, paths, and any metadata from project.json. Use this to see what projects exist before routing a session to one.",
            "parameters": {"type": "object", "properties": {}},
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
    {
        "type": "function",
        "function": {
            "name": "check_update",
            "description": "Check if a woltspace platform update is available. Use when someone asks 'is there an update?', 'any updates?', 'check for updates', or 'is woltspace up to date?'. Returns whether the platform is current or behind, and suggests routing to a rodent if an update is available.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wolf_schedules",
            "description": "List all wolf cron schedules — what's scheduled, when it runs, last run time. Use this when someone asks about scheduled tasks, crons, reminders, or recurring jobs.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fire_wolf",
            "description": "Fire a specific wolf cron immediately by name, ignoring its schedule. Use when someone says 'run the digest now', 'fire the weekly review', 'trigger X'. Check wolf_schedules first to see available cron names.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The cron name to fire (must match a name in wolf.json).",
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wolf_jobs",
            "description": "Show recent wolf job log — what crons fired, when, whether they started successfully. Use when someone asks 'did the digest run?', 'what did wolf do?', 'any failed jobs?', or 'wolf log'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "Number of recent job events to return (default 10).",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_issue",
            "description": "Open a GitHub issue on the woltspace repo. Use when someone asks to file a bug, log an idea, or track something for later. Beavers will pick these up asynchronously.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Short issue title (e.g. 'wolf notifications should include session link').",
                    },
                    "body": {
                        "type": "string",
                        "description": "Issue body with context, motivation, and rough shape of the fix. Markdown supported.",
                    },
                    "labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional labels to apply (e.g. ['from-dog', 'bug']). Labels must already exist in the repo.",
                    },
                },
                "required": ["title"],
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
    tool_calls_log = []  # deterministic log of every tool call fired
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

            # Log with result so adapters can extract session URLs
            log_entry = {"tool": name, "args": args}
            try:
                parsed = json.loads(tool_result)
                if isinstance(parsed, dict) and parsed.get("url"):
                    log_entry["url"] = parsed["url"]
                if isinstance(parsed, dict) and parsed.get("name"):
                    log_entry["session"] = parsed["name"]
                if isinstance(parsed, dict) and parsed.get("creature"):
                    log_entry["creature"] = parsed["creature"]
            except (json.JSONDecodeError, TypeError):
                pass
            tool_calls_log.append(log_entry)

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": tool_result,
            })

            # Track terminal tools that define the response type
            if name in ("claude_code", "new_session"):
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

    _bot_log("response", {"type": result_type, "text": text[:500], "rounds": rounds_used, "tool_calls": len(tool_calls_log)})

    return {
        "type": result_type,
        "text": text,
        "history_messages": history_messages,
        "tool_calls_log": tool_calls_log,
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
