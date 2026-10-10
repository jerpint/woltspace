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
  - sessions_with_agent_process() — its batched form, for whole-list liveness

Adding a harness = adding one entry to HARNESSES. Nothing else should need
to know how a harness spells its flags.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from env_compat import get_env
from session_runtime import RuntimeHandle, get_runtime
from execution_policy import policy_mode
from skills_sync import COPY_DELIVERY, PLUGIN_DELIVERY

DEFAULT_HARNESS = "claude"

# Wrappers resolved relative to this file so the dev clone drives its own
# bin/ instead of production's.
_BIN_DIR = Path(__file__).resolve().parent.parent / "bin"
WCLAUDE = str(_BIN_DIR / "wclaude")
WCODEX = str(_BIN_DIR / "wcodex")
WOPENCODE = str(_BIN_DIR / "wopencode")
WPI = str(_BIN_DIR / "wpi")

_ROLLOUT_UUID_RE = re.compile(
    r"rollout-.*-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$"
)

logger = logging.getLogger(__name__)

MODEL_CACHE_MAX_AGE = 24 * 60 * 60
_model_refreshing: set[str] = set()
_model_refresh_lock = threading.Lock()


def _parse_codex_models(output: str) -> list[dict]:
    """Parse the intentionally unstable ``codex debug models`` response."""
    try:
        data = json.loads(output)
        models = data["models"]
        if not isinstance(models, list):
            return []
        out = []
        for model in models:
            if (
                not isinstance(model, dict)
                or model.get("visibility") != "list"
                or model.get("upgrade") is not None
            ):
                continue
            slug = model.get("slug")
            label = model.get("display_name")
            if (
                not isinstance(slug, str) or not slug
                or not isinstance(label, str) or not label
            ):
                return []
            out.append({"id": slug, "label": label})
        return out
    except (json.JSONDecodeError, KeyError, TypeError):
        return []


def _discover_codex_models() -> list[dict]:
    """Best-effort Codex catalog discovery; every failure is an empty result."""
    try:
        result = subprocess.run(
            ["codex", "debug", "models"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode != 0:
            return []
        return _parse_codex_models(result.stdout)
    except (OSError, subprocess.SubprocessError):
        return []


def _claude_command(entry: dict, mode: str, *, session_id: str = "",
                    session_name: str = "", model: str = "", prompt: str = "",
                    resume_id: str = "", execution_policy=None) -> str:
    """Build a Claude Code command line. Mirrors the historical invocations exactly."""
    wrapper = entry["wrapper"]
    if mode == "login":
        return f"{wrapper} /login"

    parts = [wrapper]
    if policy_mode(execution_policy) == "auto":
        parts.append("--dangerously-skip-permissions")
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


_codex_no_daemon: bool | None = None


def codex_supports_no_daemon() -> bool:
    """Whether the installed codex CLI accepts --no-daemon.

    Older codex has no daemon and would reject the flag, so ask its help. This
    runs in `session-reg prepare`, once per spawn, on the session's own PATH.
    If the probe itself fails (codex missing, timeout) the command stays as it
    was before this flag existed: an unknown codex keeps working, it just may
    share the daemon.
    """
    global _codex_no_daemon
    if _codex_no_daemon is None:
        try:
            out = subprocess.run(
                ["codex", "--help"], capture_output=True, text=True, timeout=15,
            ).stdout
            _codex_no_daemon = "--no-daemon" in out
        except (OSError, subprocess.SubprocessError):
            _codex_no_daemon = False
    return _codex_no_daemon


def _codex_command(entry: dict, mode: str, *, session_id: str = "",
                   session_name: str = "", model: str = "", prompt: str = "",
                   resume_id: str = "", execution_policy=None) -> str:
    """Build a Codex CLI command line (verified against codex-cli 0.144).

    Codex can't preset a session id at spawn — run-session.sh discovers the
    rollout id after launch (see discover_session_id). A resume without a
    stored id falls back to a fresh session rather than guessing --last,
    which is wrong under concurrent sessions.
    """
    wrapper = entry["wrapper"]
    if mode == "login":
        # Device-code flow — no browser callback inside the container
        return f"{wrapper} login --device-auth"
    if mode not in ("spawn", "resume"):
        raise ValueError(f"unknown mode: {mode}")

    parts = [wrapper]
    if mode == "resume" and resume_id:
        parts += ["resume", resume_id]
    # Newer codex runs every session's tools inside one shared app-server
    # daemon. Its tool calls inherit the environment of whichever session
    # started the daemon (so they carry another session's identity), and when
    # the daemon restarts (it auto-updates) it reloads threads without our
    # sandbox flag, silently dropping them to workspace-write with no network.
    # Each woltspace session runs its own codex process instead.
    if codex_supports_no_daemon():
        parts.append("--no-daemon")
    # Codex's own help: "Intended solely for running in environments that are
    # externally sandboxed" — which is exactly the woltspace container.
    if policy_mode(execution_policy) == "auto":
        parts.append("--dangerously-bypass-approvals-and-sandbox")
    if model:
        parts += ["-m", model]
    if prompt:
        parts.append(prompt)
    return " ".join(shlex.quote(p) for p in parts)


def _codex_session_dirs(wolt: str) -> list[tuple[Path, bool]]:
    """Where codex may have written this wolt's rollouts, as (dir, shared).

    With per-wolt isolation wcodex points CODEX_HOME at <wolt>/.codex. Natively
    codex keeps the host's own CODEX_HOME (default ~/.codex), shared by every
    wolt, so a rollout there only counts when its cwd is this session's dir.
    """
    wolts_dir = Path(get_env("WOLTSPACE_WOLTS_DIR", "/workspace/wolts"))
    own = wolts_dir / wolt / ".codex" / "sessions"
    host = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"
    dirs = [(own, False)]
    if host.resolve() != own.resolve():
        dirs.append((host, True))
    return [(d, shared) for d, shared in dirs if d.exists()]


def _codex_rollouts(data: dict, since: float, until: float | None = None) -> list[tuple[float, str, bool]]:
    """(start, id, cwd_matches) for rollouts of this wolt that started in [since, until]."""
    wolt = data.get("wolt", "")
    if not wolt:
        return []
    session_dir = data.get("dir", "")
    found = []
    for sessions_dir, shared in _codex_session_dirs(wolt):
        for f in sessions_dir.glob("**/rollout-*.jsonl"):
            m = _ROLLOUT_UUID_RE.search(f.name)
            if not m:
                continue
            try:
                if f.stat().st_mtime < since:
                    continue  # last written before the window opened
                with f.open() as fh:
                    payload = json.loads(fh.readline()).get("payload", {})
            except (OSError, json.JSONDecodeError, AttributeError):
                continue
            try:
                from datetime import datetime
                start = datetime.fromisoformat(payload.get("timestamp", "").replace("Z", "+00:00")).timestamp()
            except (ValueError, AttributeError):
                start = f.stat().st_mtime
            if start < since or (until is not None and start > until):
                continue
            matches = bool(session_dir) and payload.get("cwd") == session_dir
            if shared and not matches:
                continue  # another wolt's (or another folder's) conversation
            found.append((start, m.group(1), matches))
    return sorted(found, reverse=True)


def _codex_discover_session_id(
    data: dict, since: float, taken: set[str] | None = None,
) -> str | None:
    """Find the rollout id codex assigned to a just-spawned session.

    Codex writes $CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl at
    session start. Returns the newest rollout that started after `since`,
    preferring one whose recorded cwd matches the session dir (disambiguates
    concurrent sessions of the same wolt in different dirs).
    """
    taken = taken or set()
    found = _codex_rollouts(data, since)
    for _, sid, matches in found:
        if matches and sid not in taken:
            return sid
    # Old records without a workdir cannot prove a cwd match. Preserve their
    # per-wolt-home discovery, but never use a cwd-mismatched fallback when the
    # spawning session has an expected workdir.
    if not data.get("dir"):
        return next((sid for _, sid, _ in found if sid not in taken), None)
    return None


def _codex_recover_session_id(data: dict, taken: set[str]) -> str | None:
    """Recover the id of a session whose spawn-time discovery missed it.

    Only a rollout in the session's own dir that started within a few minutes
    of the session, and that no other session already owns. Two candidates is
    ambiguous: better unresumable than resumed into someone else's conversation.
    """
    created = data.get("created_at") or 0
    if not created:
        return None
    found = [sid for _, sid, matches in _codex_rollouts(data, created - 30, created + 180)
             if matches and sid not in taken]
    return found[0] if len(found) == 1 else None


def _opencode_command(entry: dict, mode: str, *, session_id: str = "",
                      session_name: str = "", model: str = "", prompt: str = "",
                      resume_id: str = "", execution_policy=None) -> str:
    """Build an opencode CLI command line (verified against opencode 1.18.3).

      - The interactive TUI (root command, attachable in tmux) accepts -m/--model
        (format provider/model), -s/--session, -c/--continue, and --prompt.
        We deliberately do NOT pass --prompt: the TUI dispatches it before model
        resolution finishes, so the opening message goes to the fallback default
        model (first env-detected provider) instead of the pin — and a prompt
        starting with "/" opens the TUI's command palette and never submits
        (both benched live, 2026-08-08). The boot prompt is instead stamped as
        pending_boot_prompt by prepare_session_command and pasted in by
        deliver_boot_prompt once the TUI has painted ("prompt_via_paste" below).
      - opencode can't preset a session id at spawn — like codex, it assigns its
        own `ses_...` id, so run-session.sh discovers it after launch (see
        _opencode_discover_session_id). Resume is `--session <id>` (verified:
        restores the full conversation thread).
      - `--auto` auto-approves permissions (the root command's YOLO flag, the
        opencode equivalent of claude's --dangerously-skip-permissions and
        codex's --dangerously-bypass...). VERIFIED live: without it opencode 1.18.3
        DOES prompt (e.g. to access the platform skills dir on boot), so it is
        NOT allow-all by default — every session launches with --auto so wolts
        run unattended like every other harness.
    """
    wrapper = entry["wrapper"]
    if mode == "login":
        # `opencode auth login` is interactive (provider picker; Claude Pro/Max
        # and ChatGPT open a browser — not container-friendly). The seed flow in
        # wopencode is the real containerizable path; this is the manual fallback.
        return f"{wrapper} auth login"
    if mode not in ("spawn", "resume"):
        raise ValueError(f"unknown mode: {mode}")

    # --auto = full permissions, no approval prompts (unattended, like all wolts)
    # No --prompt ever — the boot prompt arrives via deliver_boot_prompt (see
    # docstring); a CLI prompt would race model resolution and strand on "/".
    parts = [wrapper]
    if policy_mode(execution_policy) == "auto":
        parts.append("--auto")
    if mode == "resume" and resume_id:
        parts += ["--session", resume_id]
    if model:
        parts += ["--model", model]
    return " ".join(shlex.quote(p) for p in parts)


def _opencode_discover_session_id(data: dict, since: float) -> str | None:
    """Find the ses_ id opencode assigned to a just-spawned session.

    Verified live against opencode 1.18.3: sessions live in a SQLite db
    (<wolt>/.local/share/opencode/opencode.db), NOT in per-session JSON files —
    so we ask opencode itself via `session list --format json`, run with the
    wolt's HOME/XDG (mirroring wopencode) so it reads that wolt's db. Each entry
    is {id: "ses_...", created/updated: <ms epoch>, directory: <cwd>, ...}.
    Returns the id of the newest session created at/after `since` whose recorded
    directory matches the session dir (disambiguates concurrent sessions),
    falling back to newest-in-dir, then newest overall.
    """
    wolt = data.get("wolt", "")
    if not wolt:
        return None
    wolts_dir = Path(get_env("WOLTSPACE_WOLTS_DIR", "/workspace/wolts"))
    wolt_home = wolts_dir / wolt
    if not (wolt_home / ".local" / "share" / "opencode").exists():
        return None

    env = dict(os.environ)
    env["HOME"] = str(wolt_home)
    env["XDG_DATA_HOME"] = str(wolt_home / ".local" / "share")
    env["XDG_CONFIG_HOME"] = str(wolt_home / ".config")
    # `session list` is project-scoped by cwd (opencode derives the project from
    # the working dir), so run it FROM the session's dir or it returns nothing.
    run_cwd = data.get("dir") or str(wolt_home)
    try:
        proc = subprocess.run(
            ["opencode", "session", "list", "--format", "json"],
            env=env, cwd=run_cwd, capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        sessions = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None

    def _ts(s):  # created/updated are ms epoch
        return s.get("created") or s.get("updated") or 0

    valid = [s for s in sessions
             if isinstance(s, dict) and str(s.get("id", "")).startswith("ses_")]
    valid.sort(key=_ts, reverse=True)
    if not valid:
        return None

    # Only consider sessions created at/after `since` — never return a stale
    # session from a previous run while THIS spawn's session hasn't landed yet
    # (the poller keeps trying until it does). `valid` is newest-first.
    since_ms = since * 1000
    after = [s for s in valid if _ts(s) >= since_ms]
    if not after:
        return None
    session_dir = data.get("dir", "")
    if session_dir:
        dir_match = [s for s in after if s.get("directory") == session_dir]
        if dir_match:
            return dir_match[0]["id"]
    return after[0]["id"]


def _pi_command(entry: dict, mode: str, *, session_id: str = "",
                session_name: str = "", model: str = "", prompt: str = "",
                resume_id: str = "", execution_policy=None) -> str:
    """Build a Pi CLI command line (groundwork against Pi 0.87.1).

    Pi accepts a caller-supplied UUID, so Woltspace owns the session identity
    from first spawn and never has to discover it from Pi's JSONL store.
    `--approve` trusts project-local resources for this run; it is not a tool
    permission or sandbox flag. Pi deliberately has no built-in tool approval
    system, so isolation remains the launcher's responsibility.

    This command shape is covered offline only. A live TUI/model bench is a
    separate, owner-approved gate before Pi is considered production-ready.
    """
    wrapper = entry["wrapper"]
    if mode == "login":
        # Login is a slash command inside Pi's interactive UI. Starting the TUI
        # without injecting `/login` avoids treating an unverified argv command
        # path as a prompt (and cannot contact a provider by itself).
        return wrapper
    if mode not in ("spawn", "resume"):
        raise ValueError(f"unknown mode: {mode}")

    parts = [wrapper, "--approve"]
    if mode == "spawn" and session_id:
        parts += ["--session-id", session_id]
    elif mode == "resume" and resume_id:
        parts += ["--session", resume_id]
    if session_name and mode == "spawn":
        parts += ["--name", session_name]
    if model:
        parts += ["--model", model]
    if prompt:
        parts.append(prompt)
    return " ".join(shlex.quote(p) for p in parts)


HARNESSES = {
    "claude": {
        "wrapper": WCLAUDE,
        "command": _claude_command,
        # display metadata for pickers/badges (exposed via the API)
        "label": "Claude Code",
        "emoji": "🟠",
        # comm names that count as "the agent is running" in a session's process tree
        "process_names": {"claude"},
        # creature tier → default model flag value (the seed; woltspace.json may override)
        "models": {
            "raccoon": "opus",
            "beaver": "sonnet",
            "otter": "haiku",
            "rodent": "opus",  # legacy type — treated as raccoon
            "wolf": "sonnet",
        },
        # every model a wolt may be pinned to on this harness (Free binding: any
        # model pickable for any tier). Seed list — woltspace.json can add/remove.
        "model_catalog": [
            {"id": "opus", "label": "Opus"},
            {"id": "sonnet", "label": "Sonnet"},
            {"id": "haiku", "label": "Haiku"},
            # `claude --model fable` alias verified live (2026-07-16)
            {"id": "fable", "label": "Fable"},
        ],
        # how a skill is invoked inside a prompt
        "skill_invoke": "/{name}",
        # platform skills arrive through the woltspace plugin, which namespaces
        # every skill under the marketplace name. Verified live 2026-09-06.
        "platform_skill_invoke": "/woltspace:{name}",
        "instructions_file": "CLAUDE.md",
        "auth_file": ".claude/.credentials.json",
        # claude accepts --session-id at spawn; codex assigns its own
        "preset_session_id": True,
        "discover_session_id": None,
        # claude's TUI accepts paste + immediate Enter (see _tmux_paste)
        "paste_settle": 0.0,
    },
    "codex": {
        "wrapper": WCODEX,
        "command": _codex_command,
        "label": "Codex",
        "emoji": "⬛",
        "process_names": {"codex"},
        # From the live /model picker (codex-cli 0.144.4, 2026-07):
        # gpt-5.5 "frontier, complex work", gpt-5.6-terra "balanced, everyday"
        # (the default), gpt-5.6-luna "fast and affordable".
        "models": {
            "raccoon": "gpt-5.5",
            "beaver": "gpt-5.6-terra",
            "otter": "gpt-5.6-luna",
            "rodent": "gpt-5.5",
            "wolf": "gpt-5.6-terra",
        },
        "model_catalog": [
            {"id": "gpt-5.5", "label": "GPT-5.5"},
            {"id": "gpt-5.6-terra", "label": "GPT-5.6 Terra"},
            {"id": "gpt-5.6-luna", "label": "GPT-5.6 Luna"},
            # VERIFY live: exact id for "Sol" from codex's /model picker
            {"id": "gpt-5.6-sol", "label": "GPT-5.6 Sol"},
        ],
        "discover_models": _discover_codex_models,
        # codex's native skill mention (the mentions_v2 feature) — resolves a
        # discovered skill for real. `$name` only worked by the model choosing to
        # read the SKILL.md itself; `@` is the reliable trigger. Verified live 2026-07-16.
        "skill_invoke": "@{name}",
        # codex auto-namespaces a skill tree that carries .claude-plugin/ at its
        # root under the plugin name — so the platform skills it discovers
        # through the symlink answer to `woltspace:<base>`, the same name claude
        # gets from the plugin. Verified live 2026-09-06.
        "platform_skill_invoke": "@woltspace:{name}",
        "instructions_file": "AGENTS.md",
        "auth_file": ".codex/auth.json",
        "preset_session_id": False,
        "discover_session_id": _codex_discover_session_id,
        "recover_session_id": _codex_recover_session_id,
        # codex's TUI folds an Enter arriving right after a paste into the
        # paste (message stays in the composer). Verified live: 0.5s settle
        # before the Enter keystroke submits reliably.
        "paste_settle": 0.5,
    },
    "opencode": {
        "wrapper": WOPENCODE,
        "command": _opencode_command,
        "label": "opencode",
        "emoji": "🟦",
        "process_names": {"opencode"},
        # opencode is a multi-provider engine with hundreds of models across
        # providers — a curated whitelist can't keep up, so model pins are
        # FREEFORM: any "provider/model" string is accepted and the catalog below
        # is just starter suggestions. (claude/codex stay catalog-gated.)
        "freeform_model": True,
        # Model ids are provider/model strings from opencode's models.dev catalog
        # (`opencode session`/`opencode models` list them). Defaulting to the
        # OpenAI provider — VERIFIED live end-to-end (opencode 1.18.3, spawn +
        # resume + skills) using OPENAI_API_KEY from the environment, which the
        # container passes through to sessions. Swapping provider is a one-line
        # change per tier: anthropic/* (needs Claude Max OAuth — untested here),
        # openrouter/<vendor>/<model> (one key, 341 models — needs a valid key),
        # opencode/* (Zen), etc.
        "models": {
            "raccoon": "openai/gpt-4o",        # frontier / thinker
            "beaver": "openai/gpt-4o",           # balanced / builder
            "otter": "openai/gpt-4o-mini",       # fast / quick
            "rodent": "openai/gpt-4o",           # legacy — treated as raccoon
            "wolf": "openai/gpt-4o-mini",
        },
        # Selectable models for the picker (new-main integration).
        "model_catalog": [
            {"id": "openai/gpt-4o", "label": "GPT-4o"},
            {"id": "openai/gpt-4o-mini", "label": "GPT-4o mini"},
            {"id": "openai/gpt-4.1", "label": "GPT-4.1"},
        ],
        # opencode mirrors Claude Code conventions (reads ~/.claude/skills and
        # CLAUDE.md), and a /skill mention triggers the skill natively — but only
        # when it arrives as a PASTE. Typed/CLI-injected prompts starting with
        # "/" open the TUI command palette ("No matching items") and never
        # submit, which is one of the two reasons boot prompts go via paste.
        "skill_invoke": "/{name}",
        # opencode is the odd one out. Benched live on 1.18.29: it discovers the
        # whole platform tree through the symlink chain, but it does NOT
        # namespace it — a platform skill answers to its bare frontmatter name
        # (`/notify`), not `woltspace:notify` the way claude and codex spell it.
        # Consequence, documented rather than defended against: on opencode a
        # wolt-owned skill named `notify` collides with the platform one, and
        # neither wins by rule. Nothing here special-cases that; a wolt that
        # names its own skill after a platform skill gets what it asked for.
        # The leading space is the palette defuse — a pasted message starting
        # with "/" opens the command palette instead of submitting.
        "platform_skill_invoke": " /{name}",
        # The TUI can't take the boot prompt on the CLI (see _opencode_command
        # docstring). prepare_session_command stamps it; deliver_boot_prompt
        # pastes it once the marker below shows in the pane (the composer hint
        # bar, painted with the rest of the TUI after model resolution —
        # re-verify the string when bumping the opencode version).
        "prompt_via_paste": True,
        "tui_ready_marker": "ctrl+p commands",
        # A pasted message starting with "/" opens the command palette instead
        # of submitting; _guard_paste_text prepends a space to defuse it.
        "leading_slash_opens_palette": True,
        # opencode's TUI drops newlines from a pasted message (joins lines with
        # no separator → run-on text). Flatten \n → space so a multi-line
        # message (IWCL attribution) stays readable. claude/codex are
        # paste-aware and keep pasted newlines, so they leave this unset.
        "flatten_paste_newlines": True,
        # opencode reads AGENTS.md as primary, CLAUDE.md as a documented
        # fallback. wopencode symlinks AGENTS.md -> CLAUDE.md for parity with
        # codex; the fallback means it would work even without the symlink.
        "instructions_file": "AGENTS.md",
        # auth.json lives under the data dir, not the config dir.
        "auth_file": ".local/share/opencode/auth.json",
        # opencode assigns its own ses_ id — discover after launch, like codex.
        "preset_session_id": False,
        "discover_session_id": _opencode_discover_session_id,
        # opencode TUI paste/submit: --prompt on the root command auto-submits
        # (verified live — the opening prompt ran without a manual Enter). Kept at
        # codex's proven 0.5s settle for the resume-delivery paste path; can drop
        # to 0.0 if live IWCL delivery proves it submits cleanly.
        "paste_settle": 0.5,
    },
    "pi": {
        "wrapper": WPI,
        "command": _pi_command,
        "label": "Pi",
        "emoji": "🥧",
        "process_names": {"pi"},
        # First exploration is deliberately OpenRouter-only. `openrouter/auto`
        # is stable while individual routed model ids evolve; all three tiers
        # remain identical until a no-model-call catalog review chooses explicit
        # defaults. Woltspace still permits an explicit OpenRouter model pin.
        "freeform_model": True,
        "model_prefixes": ("openrouter/",),
        "models": {
            "raccoon": "openrouter/auto",
            "beaver": "openrouter/auto",
            "otter": "openrouter/auto",
            "rodent": "openrouter/auto",
            "wolf": "openrouter/auto",
        },
        "model_catalog": [
            {"id": "openrouter/auto", "label": "OpenRouter Auto"},
        ],
        # Pi implements Agent Skills and explicitly invokes them as
        # `/skill:<frontmatter-name>`. It recursively discovers .agents/skills,
        # so the existing Woltspace bridge works for copied and plugin delivery.
        "skill_invoke": "/skill:{name}",
        # Like opencode, Pi does not namespace the platform symlink tree.
        "platform_skill_invoke": "/skill:{name}",
        "instructions_file": "AGENTS.md",
        "auth_file": ".pi/agent/auth.json",
        "preset_session_id": True,
        "discover_session_id": None,
        # UNVERIFIED live: tune if Pi's TUI folds immediate Enter into a paste.
        "paste_settle": 0.5,
    },
}

# Process names that mean "still launching" — the wrapper chain before the
# agent process exists. Shared across harnesses (run-session.sh is ours, not
# theirs). These are matched against the *script* an interpreter is running,
# not against `comm`: a shebang script is only ever reported as its
# interpreter (`bash`), which is why the runtime reads argv for these.
LAUNCHING_NAMES = {"run-session.sh", "run-session"}


def resolve_harness(name: str | None) -> str:
    """Normalize a harness name — unknown/empty falls back to the default."""
    return name if name in HARNESSES else DEFAULT_HARNESS


def get_harness(name: str | None) -> dict:
    """Get a harness table entry, falling back to the default harness."""
    return HARNESSES[resolve_harness(name)]


# The namespace a platform skill answers to once it is delivered as the
# woltspace plugin (claude) or as a `.claude-plugin/`-rooted tree (codex).
PLATFORM_SKILL_NAMESPACE = "woltspace"

# The copy-sync spelling: skills are copied into the wolt under a `woltspace-`
# prefix and invoked by that whole name (`/woltspace-notify` on claude,
# `@woltspace-notify` on codex). Most of the colony is still here.
LEGACY_PLATFORM_SKILL_PREFIX = "woltspace-"


def platform_skill_invoke(harness: str | None, name: str,
                          delivery: str = COPY_DELIVERY) -> str:
    """How this harness invokes the platform skill `name` for THIS wolt.

    The spelling is a function of two things, and getting either wrong hands a
    session a skill that does not exist:

      delivery == "copy" (the default, and most of the colony)
          The skill is a copy sitting in the wolt's own skills directory under
          `woltspace-<name>`, with no namespace of its own. It is invoked the
          way any wolt-owned skill is — `skill_invoke` — with that full
          prefixed name.

      delivery == "plugin"
          claude and codex namespace it: `/woltspace:<name>`, `@woltspace:<name>`.
          opencode namespaces nothing and answers to the bare name (see its
          table entry). A harness with no platform template falls back to
          `skill_invoke` with the bare name.

    `COPY_DELIVERY` is the default on purpose: a caller with no wolt in hand
    cannot know, and the un-ratcheted spelling is the one almost every wolt
    currently understands.
    """
    entry = get_harness(harness)
    if delivery != PLUGIN_DELIVERY:
        return entry["skill_invoke"].format(
            name=f"{LEGACY_PLATFORM_SKILL_PREFIX}{name}")
    template = entry.get("platform_skill_invoke") or entry["skill_invoke"]
    return template.format(name=name)


def _model_overlay(harness: str | None) -> dict:
    """woltspace.json's per-harness model overrides, or {} if unset/malformed.

    Shape: woltspace.json -> "harness" -> "models" -> "<harness>" ->
        {"catalog": [<id> | {"id":..,"label":..}, ...], "tiers": {<tier>: <model>}}
    Everything is optional; a missing/broken file just yields the built-in seed.
    """
    try:
        cfg = json.loads(_woltspace_json_path().read_text())
        models = cfg.get("harness", {}).get("models", {})
        return models.get(resolve_harness(harness), {}) or {}
    except (json.JSONDecodeError, OSError, AttributeError):
        return {}


def _model_cache_path(harness: str) -> Path:
    return _woltspace_json_path().parent / ".space" / "models" / f"{harness}.json"


def _read_model_cache(harness: str) -> tuple[list[dict], float | None]:
    try:
        data = json.loads(_model_cache_path(harness).read_text())
        fetched_at = data["fetched_at"]
        models = data["models"]
        if not isinstance(fetched_at, (int, float)) or not isinstance(models, list):
            return [], None
        clean = []
        for model in models:
            if not isinstance(model, dict) or not isinstance(model.get("id"), str):
                return [], None
            label = model.get("label")
            if not model["id"] or not isinstance(label, str) or not label:
                return [], None
            clean.append({"id": model["id"], "label": label})
        return clean, float(fetched_at)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return [], None


def _model_cache_stale(harness: str, *, now: float | None = None) -> bool:
    _, fetched_at = _read_model_cache(harness)
    current = now if now is not None else time.time()
    return fetched_at is None or current - fetched_at >= MODEL_CACHE_MAX_AGE


def _write_model_cache(
    harness: str,
    models: list[dict],
    *,
    fetched_at: float | None = None,
) -> None:
    cache = _model_cache_path(harness)
    cache.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {
        "fetched_at": fetched_at if fetched_at is not None else time.time(),
        "models": models,
    }
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", dir=cache.parent, prefix=f".{harness}.", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, separators=(",", ":"))
        temporary.chmod(0o600)
        os.replace(temporary, cache)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _refresh_model_cache(harness: str) -> None:
    try:
        discover = get_harness(harness).get("discover_models")
        if discover:
            models = discover()
            if models:
                _write_model_cache(harness, models)
    finally:
        with _model_refresh_lock:
            _model_refreshing.discard(harness)


def refresh_model_catalogs(*, force: bool = False) -> None:
    """Schedule stale harness discoveries without waiting for subprocesses."""
    for harness, entry in HARNESSES.items():
        if (
            not entry.get("discover_models")
            or (not force and not _model_cache_stale(harness))
        ):
            continue
        with _model_refresh_lock:
            if harness in _model_refreshing:
                continue
            _model_refreshing.add(harness)
        threading.Thread(
            target=_refresh_model_cache,
            args=(harness,),
            name=f"woltspace-models-{harness}",
            daemon=True,
        ).start()


def model_catalog(harness: str | None) -> list[dict]:
    """Return overlay, or cached discovery merged with the stable seed."""
    resolved = resolve_harness(harness)
    seed = get_harness(resolved).get("model_catalog", [])
    discovered, _ = _read_model_cache(resolved)
    merged = [dict(m) for m in discovered]
    seen = {m["id"] for m in merged}
    merged.extend(dict(m) for m in seed if m["id"] not in seen)
    ov_catalog = _model_overlay(harness).get("catalog")
    if ov_catalog is None:
        return merged
    label_by_id = {m["id"]: m.get("label", m["id"]) for m in reversed(merged)}
    out = []
    for item in ov_catalog:
        # Only a non-empty string is a model id; anything else is not an entry.
        model_id = item.get("id") if isinstance(item, dict) else item
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        label = item.get("label") if isinstance(item, dict) else None
        if not isinstance(label, str) or not label:
            label = label_by_id.get(model_id, model_id)
        out.append({"id": model_id, "label": label})
    if not out:
        # A catalog that offers nothing would leave every session without a
        # valid model. Treat it as not set rather than run outside it.
        logger.warning("Ignoring empty model catalog for %s in woltspace.json", resolved)
        return merged
    return out


def is_valid_model(harness: str | None, model: str | None) -> bool:
    """True if `model` is usable for this harness.

    Freeform harnesses (freeform_model=True, e.g. opencode) accept non-empty
    provider/model strings — their catalog is suggestions, not a whitelist.
    An optional model_prefixes tuple can bound that freedom to selected providers.
    Catalog-gated harnesses (claude, codex) require catalog membership.
    """
    if not model:
        return False
    entry = get_harness(harness)
    if entry.get("freeform_model"):
        prefixes = entry.get("model_prefixes")
        return not prefixes or model.startswith(tuple(prefixes))
    return any(m["id"] == model for m in model_catalog(harness))


# Where each tier sits in a harness's own ranked model list: the thinker gets
# the first model, the builder the second, the quick one the third. The
# internal aliases follow the tier they have always shared a default with.
_RANKED_TIER_POSITION = {"raccoon": 0, "rodent": 0, "beaver": 1, "wolf": 1, "otter": 2}


def automatic_tier_model(harness: str | None, tier: str | None) -> str | None:
    """A tier's default before any lodge setting.

    When the harness reported its own model list, the defaults follow that
    list's order, so they keep up with the harness instead of naming whatever
    was current when this table was written. Without a usable list (no
    discovery, discovery failed, fewer than three models) the built-in seed
    applies.
    """
    if not tier:
        return None
    resolved = resolve_harness(harness)
    entry = get_harness(resolved)
    seed = entry["models"].get(tier)
    if entry.get("freeform_model"):
        return seed  # no fixed list to stay inside; any provider/model is valid
    offered = [m["id"] for m in model_catalog(resolved)]
    position = _RANKED_TIER_POSITION.get(tier)
    if position is not None and entry.get("discover_models"):
        discovered, _ = _read_model_cache(resolved)
        ranked = [m["id"] for m in discovered if m["id"] in offered]
        if len(ranked) >= len(PICKER_TIERS):
            return ranked[position]
    if seed is None or seed in offered or not offered:
        return seed  # an unknown tier has no default at all
    # The lodge's catalog excludes the usual default: stay inside the catalog
    # rather than hand a session a model the owner removed.
    if position is None:
        return offered[0]
    return offered[min(position, len(offered) - 1)]


def tier_default_model(harness: str | None, tier: str | None) -> str | None:
    """The default model for a tier on this harness: the lodge's saved choice
    (woltspace.json "tiers") if there is one, else the automatic default."""
    if not tier:
        return None
    overlay_tiers = _model_overlay(harness).get("tiers", {})
    saved = overlay_tiers.get(tier) if isinstance(overlay_tiers, dict) else None
    # A saved choice the catalog no longer offers is ignored, not launched.
    if saved and is_valid_model(harness, saved):
        return saved
    return automatic_tier_model(harness, tier)


def creature_model(harness: str | None, creature: str | None) -> str | None:
    """Map a creature tier to this harness's default model flag value."""
    return tier_default_model(harness, creature)


def resolve_model(harness: str | None, creature: str | None,
                  pinned: str | None = None) -> str | None:
    """The model a session actually spawns with.

    A pinned model wins ONLY if it's valid for the resolved harness — a pin is
    harness-scoped ("opus" means nothing to codex), so switching engines drops a
    now-invalid pin back to the tier default. No invalid model reaches spawn.
    For freeform harnesses (opencode) any non-empty pin is valid, so a
    user-typed "provider/model" is honored as-is.
    """
    if pinned and is_valid_model(harness, pinned):
        return pinned
    return creature_model(harness, creature)


# Tier order + labels for pickers (raccoon/beaver/otter are the user-facing
# tiers; rodent/wolf are internal aliases and stay out of the UI list).
PICKER_TIERS = [
    ("raccoon", "thinker"),
    ("beaver", "builder"),
    ("otter", "quick"),
]


def harness_metadata() -> list[dict]:
    """Public, JSON-safe view of the harness table for pickers/badges.

    Only display + model data — no wrappers, functions, or file paths.
    """
    # A long-running lodge may outlive the cache TTL. Picker reads only schedule
    # the already-deduplicated background refresh; discovery never blocks this
    # request and spawn paths do not call it.
    refresh_model_catalogs()
    out = []
    for hid, entry in HARNESSES.items():
        out.append({
            "id": hid,
            "label": entry.get("label", hid),
            "emoji": entry.get("emoji", ""),
            # per-tier default model (merged view — reflects woltspace.json overrides)
            "models": {tier: tier_default_model(hid, tier) for tier, _ in PICKER_TIERS},
            # what each tier gets when the lodge has saved no choice of its own
            "automatic_models": {
                tier: automatic_tier_model(hid, tier) for tier, _ in PICKER_TIERS
            },
            "saved_models": _saved_tier_models(hid),
            # full selectable list for the model picker (merged view)
            "catalog": model_catalog(hid),
            "freeform_model": bool(entry.get("freeform_model")),
        })
    return out


# --- Space-level default (woltspace.json "harness.default") ---------------
# The lodge default new sessions fall back to when a wolt has no override.
# Lives in woltspace.json (structured lodge settings), not .env.

def _woltspace_json_path() -> Path:
    return Path(get_env("WOLTSPACE_WOLTS_DIR", "/workspace/wolts")) / "woltspace.json"


def get_default_harness() -> str:
    """Read the lodge default harness. Falls back to the platform default."""
    try:
        cfg = json.loads(_woltspace_json_path().read_text())
        return resolve_harness(cfg.get("harness", {}).get("default"))
    except (json.JSONDecodeError, OSError, AttributeError):
        return DEFAULT_HARNESS


def _load_lodge_settings_for_write() -> tuple[Path, dict]:
    """woltspace.json as a dict, for a writer that will replace the file.

    A missing file starts empty. A file that exists but cannot be read or is
    not a JSON object is NEVER replaced: writing from an empty dict would
    throw away every other setting in it.
    """
    path = _woltspace_json_path()
    if not path.exists():
        return path, {}
    try:
        cfg = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(
            "the lodge settings file (woltspace.json) could not be read; nothing was saved"
        ) from exc
    if not isinstance(cfg, dict):
        raise ValueError(
            "the lodge settings file (woltspace.json) is not a JSON object; nothing was saved"
        )
    return path, cfg


def _settings_section(parent: dict, key: str) -> dict:
    """A nested settings object, created when absent, refused when it is not one."""
    if key not in parent:
        parent[key] = {}
    if not isinstance(parent[key], dict):
        raise ValueError(
            f"the lodge settings file (woltspace.json) has an unexpected {key!r} entry; nothing was saved"
        )
    return parent[key]


def _write_lodge_settings(path: Path, cfg: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    tmp.rename(path)


def set_default_harness(name: str) -> str:
    """Set the lodge default harness in woltspace.json. Returns the resolved value.

    Raises ValueError for an unknown harness, or when the settings file exists
    but cannot be read — the caller (API) surfaces it.
    """
    if name not in HARNESSES:
        raise ValueError(f"unknown harness: {name}")
    path, cfg = _load_lodge_settings_for_write()
    _settings_section(cfg, "harness")["default"] = name
    _write_lodge_settings(path, cfg)
    return name


def _saved_tier_models(harness: str) -> dict:
    """The lodge's own per-tier choices for a harness (picker tiers only)."""
    saved = _model_overlay(harness).get("tiers", {})
    if not isinstance(saved, dict):
        return {}
    return {
        tier: saved[tier] for tier, _ in PICKER_TIERS
        if isinstance(saved.get(tier), str) and saved[tier]
    }


def set_tier_defaults(harness: str, tiers: dict) -> dict:
    """Set a harness's default model per tier in woltspace.json.

    ``tiers`` maps a picker tier (raccoon, beaver, otter) to a model id. A chosen
    model is always kept, even when it is the one the tier would get anyway;
    an empty value clears the choice and the automatic default applies again.
    The whole request is validated before anything is written, and an existing
    settings file that cannot be read is never replaced. Returns the resulting
    defaults.
    """
    if harness not in HARNESSES:
        raise ValueError(f"unknown harness: {harness}")
    if not isinstance(tiers, dict) or not tiers:
        raise ValueError("tiers must be a non-empty object")
    allowed = {tier for tier, _ in PICKER_TIERS}
    for tier, model in tiers.items():
        if tier not in allowed:
            raise ValueError(f"unknown tier: {tier}")
        if model in (None, ""):
            continue
        if not isinstance(model, str) or not is_valid_model(harness, model):
            raise ValueError(f"unknown model for {harness}: {model}")
    path, cfg = _load_lodge_settings_for_write()
    section = _settings_section(cfg, "harness")
    entry = _settings_section(_settings_section(section, "models"), harness)
    saved = _settings_section(entry, "tiers")
    for tier, model in tiers.items():
        # (5) what the owner picks is what is kept; only an empty value clears.
        if model in (None, ""):
            saved.pop(tier, None)
        else:
            saved[tier] = model
    _write_lodge_settings(path, cfg)
    return {tier: tier_default_model(harness, tier) for tier, _ in PICKER_TIERS}


def build_command(harness: str | None, mode: str, **kwargs) -> str:
    """Build the full shell command for a harness.

    mode: "spawn" (fresh session), "resume", or "login".
    kwargs: session_id, session_name, model, prompt, resume_id — each harness
    uses the subset it supports.
    """
    entry = get_harness(harness)
    return entry["command"](entry, mode, **kwargs)


def _as_handle(session: str | dict | RuntimeHandle) -> RuntimeHandle:
    """Accept a session name, a registry record, or an already-built handle."""
    if isinstance(session, RuntimeHandle):
        return session
    if isinstance(session, dict):
        return RuntimeHandle.from_record(session)
    return RuntimeHandle(session, session)


def _wanted_processes(harness: str | None, include_launching: bool) -> set[str]:
    if harness is None:
        # Match ANY known harness — callers like the vulture only see a tmux
        # session and must not kill a live agent because they can't tell which
        # harness it runs.
        process_names = set().union(*(e["process_names"] for e in HARNESSES.values()))
    else:
        process_names = set(get_harness(harness)["process_names"])
    if include_launching:
        process_names |= LAUNCHING_NAMES
    return process_names


def resolve_agent_handle(session_name: str | dict | RuntimeHandle,
                         harness: str | None = None,
                         include_launching: bool = True) -> RuntimeHandle | None:
    """Locate the pane an agent is actually running in, or None.

    This is the same walk `session_has_agent_process` reports on, but it hands
    back *where* the agent was found rather than just whether it exists. Resume
    delivery uses the returned handle so detection and delivery can never
    resolve different panes — the failure that made a prompt land silently in
    whichever window the human last clicked on.
    """
    handle = _as_handle(session_name)
    return get_runtime().resolve_process_handle(
        handle, _wanted_processes(harness, include_launching)
    )


def session_has_agent_process(session_name: str | dict | RuntimeHandle,
                              harness: str | None = None,
                              include_launching: bool = True) -> bool | None:
    """Check if a tmux session has a live agent process anywhere in its tree.

    Walks every pane of the session (all windows, not just the current one)
    and the full subtree from each pane's root PID — not just direct children.
    The actual tree is: pane(bash) -> bash(run-session.sh) -> bash(wrapper) ->
    agent, so checking only direct children always misses the agent.

    harness: restrict to one harness's process names; None matches any harness.
    include_launching: also count the launching shim (run-session.sh) so a
        session that has not finished booting reads as alive.

    Returns True/False, or None when the answer is undetermined — the tmux
    session does not exist, or the process table could not be read. None is
    never "dead": the vulture treats anything but False as alive so it can
    never reap on uncertainty.
    """
    handle = _as_handle(session_name)
    runtime = get_runtime()
    return runtime.has_descendant_process(
        handle, _wanted_processes(harness, include_launching)
    )


def sessions_with_agent_process(harness: str | None = None,
                                include_launching: bool = True) -> set[str] | None:
    """Batched `session_has_agent_process`: which tmux sessions carry an agent.

    `list()` asks this of every registry record at once; asking per session
    forks tmux and ps once each, which is what made agent-accurate liveness
    look too expensive to put in the list in the first place.

    Returns a set of tmux session names, or None when undetermined (the
    process table could not be read, or the installed runtime does not
    implement the batch call). None is never "nothing is alive" — callers
    fall back to tmux presence.
    """
    batch = getattr(get_runtime(), "sessions_with_process", None)
    if batch is None:
        return None
    return batch(_wanted_processes(harness, include_launching))
