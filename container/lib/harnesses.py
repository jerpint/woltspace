"""
Harness registry — the single place that knows how to drive each CLI coding agent.

A "harness" is the CLI agent a session runs (Claude Code today; codex, opencode
later). Sessions store their harness at creation and keep it for life — a session
born on one harness always resumes on it, because conversation state doesn't
transfer between harnesses. wolt.json may set a per-wolt default via "harness".

Everything harness-specific lives here:
  - build_command() — the ONLY place harness CLI syntax (flags, spellings) exists
  - process_names   — what a live agent looks like in a process tree (liveness/vulture)
  - models          — creature tier → model flag value, per harness
  - session_has_agent_process() — the shared process-tree walker

Adding a harness = adding one entry to HARNESSES. Nothing else should need
to know how a harness spells its flags.
"""

from __future__ import annotations

import shlex
import subprocess

DEFAULT_HARNESS = "claude"

WCLAUDE = "/workspace/woltspace/container/bin/wclaude"


def _claude_command(entry: dict, mode: str, *, session_id: str = "",
                    session_name: str = "", model: str = "", prompt: str = "",
                    resume_id: str = "") -> str:
    """Build a Claude Code command line. Mirrors the historical invocations exactly."""
    wrapper = entry["wrapper"]
    if mode == "login":
        return f"{wrapper} /login"

    parts = [wrapper, "--dangerously-skip-permissions"]
    if mode == "spawn":
        if session_id:
            parts += ["--session-id", session_id]
        if session_name:
            parts += ["--name", session_name]
    elif mode == "resume":
        if resume_id:
            parts += ["--resume", resume_id]
    else:
        raise ValueError(f"unknown mode: {mode}")
    if model:
        parts += ["--model", model]
    if prompt:
        parts.append(prompt)
    return " ".join(shlex.quote(p) for p in parts)


HARNESSES = {
    "claude": {
        "wrapper": WCLAUDE,
        "command": _claude_command,
        # comm names that count as "the agent is running" in a session's process tree
        "process_names": {"claude"},
        # creature tier → model flag value
        "models": {
            "raccoon": "opus",
            "beaver": "sonnet",
            "otter": "haiku",
            "rodent": "opus",  # legacy type — treated as raccoon
            "wolf": "sonnet",
        },
        # how a skill is invoked inside a prompt
        "skill_invoke": "/{name}",
        "instructions_file": "CLAUDE.md",
        "auth_file": ".claude/.credentials.json",
    },
}

# comm names that mean "still launching" — the wrapper chain before the agent
# process exists. Shared across harnesses (run-session.sh is ours, not theirs).
LAUNCHING_NAMES = {"run-session.sh", "run-session"}


def resolve_harness(name: str | None) -> str:
    """Normalize a harness name — unknown/empty falls back to the default."""
    return name if name in HARNESSES else DEFAULT_HARNESS


def get_harness(name: str | None) -> dict:
    """Get a harness table entry, falling back to the default harness."""
    return HARNESSES[resolve_harness(name)]


def creature_model(harness: str | None, creature: str | None) -> str | None:
    """Map a creature tier to this harness's model flag value."""
    if not creature:
        return None
    return get_harness(harness)["models"].get(creature)


def build_command(harness: str | None, mode: str, **kwargs) -> str:
    """Build the full shell command for a harness.

    mode: "spawn" (fresh session), "resume", or "login".
    kwargs: session_id, session_name, model, prompt, resume_id — each harness
    uses the subset it supports.
    """
    entry = get_harness(harness)
    return entry["command"](entry, mode, **kwargs)


def session_has_agent_process(session_name: str, harness: str | None = None,
                              include_launching: bool = True) -> bool | None:
    """Check if a tmux session has a live agent process anywhere in its tree.

    Walks the full subtree from the pane's root PID — not just direct children.
    The actual tree is: pane(bash) → bash(run-session.sh) → bash(wrapper) → agent,
    so checking only direct children always misses the agent.

    include_launching also counts run-session.sh itself as alive — a session
    that is still booting has no agent process yet but must not be reaped.

    Returns True/False, or None if the tmux session doesn't exist.
    """
    process_names = set(get_harness(harness)["process_names"])
    if include_launching:
        process_names |= LAUNCHING_NAMES
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-t", session_name, "-F", "#{pane_pid}"],
            capture_output=True, text=True, check=True,
        )
        pane_pids = [p for p in result.stdout.strip().split("\n") if p]
        if not pane_pids:
            return None

        # Build full process table: pid → (ppid, comm)
        ps_result = subprocess.run(
            ["ps", "--no-headers", "-eo", "pid,ppid,comm"],
            capture_output=True, text=True,
        )
        children: dict[str, list[str]] = {}
        comms: dict[str, str] = {}
        for line in ps_result.stdout.strip().split("\n"):
            parts = line.split()
            if len(parts) >= 3:
                pid, ppid, comm = parts[0], parts[1], parts[2]
                children.setdefault(ppid, []).append(pid)
                comms[pid] = comm

        # BFS from each pane pid — True if any descendant is an agent process
        queue = list(pane_pids)
        seen: set[str] = set()
        while queue:
            cur = queue.pop()
            if cur in seen:
                continue
            seen.add(cur)
            if comms.get(cur) in process_names:
                return True
            queue.extend(children.get(cur, []))
        return False
    except subprocess.CalledProcessError:
        return None
    except Exception:
        return None
