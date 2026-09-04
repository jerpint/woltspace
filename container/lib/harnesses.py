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

import json
import os
import re
import shlex
import subprocess
from pathlib import Path

from session_runtime import RuntimeHandle, get_runtime

DEFAULT_HARNESS = "claude"

# Wrappers resolved relative to this file so the dev clone drives its own
# bin/ instead of production's.
_BIN_DIR = Path(__file__).resolve().parent.parent / "bin"
WCLAUDE = str(_BIN_DIR / "wclaude")
WCODEX = str(_BIN_DIR / "wcodex")
WOPENCODE = str(_BIN_DIR / "wopencode")

_ROLLOUT_UUID_RE = re.compile(
    r"rollout-.*-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$"
)


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


def _codex_command(entry: dict, mode: str, *, session_id: str = "",
                   session_name: str = "", model: str = "", prompt: str = "",
                   resume_id: str = "") -> str:
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
    # Codex's own help: "Intended solely for running in environments that are
    # externally sandboxed" — which is exactly the woltspace container.
    parts.append("--dangerously-bypass-approvals-and-sandbox")
    if model:
        parts += ["-m", model]
    if prompt:
        parts.append(prompt)
    return " ".join(shlex.quote(p) for p in parts)


def _codex_discover_session_id(data: dict, since: float) -> str | None:
    """Find the rollout id codex assigned to a just-spawned session.

    Codex writes $CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl at
    session start. Returns the uuid of the newest rollout created after
    `since`, preferring files whose recorded cwd matches the session dir
    (disambiguates concurrent sessions of the same wolt in different dirs).
    """
    wolt = data.get("wolt", "")
    if not wolt:
        return None
    wolts_dir = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))
    sessions_dir = wolts_dir / wolt / ".codex" / "sessions"
    if not sessions_dir.exists():
        return None

    candidates = []
    for f in sessions_dir.glob("**/rollout-*.jsonl"):
        try:
            if f.stat().st_mtime < since:
                continue
        except OSError:
            continue
        m = _ROLLOUT_UUID_RE.search(f.name)
        if m:
            candidates.append((f.stat().st_mtime, f, m.group(1)))
    if not candidates:
        return None
    candidates.sort(reverse=True)

    # Prefer a rollout whose recorded cwd matches the session dir
    session_dir = data.get("dir", "")
    if session_dir:
        for _, f, sid in candidates:
            try:
                meta = json.loads(f.read_text().split("\n", 1)[0])
            except (json.JSONDecodeError, OSError, IndexError):
                continue
            cwd = meta.get("cwd") or meta.get("payload", {}).get("cwd", "")
            if cwd == session_dir:
                return sid
    return candidates[0][2]


def _opencode_command(entry: dict, mode: str, *, session_id: str = "",
                      session_name: str = "", model: str = "", prompt: str = "",
                      resume_id: str = "") -> str:
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
    parts = [wrapper, "--auto"]
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
    wolts_dir = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))
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
            {"id": "opus", "label": "Opus 4.8"},
            {"id": "sonnet", "label": "Sonnet 5"},
            {"id": "haiku", "label": "Haiku 4.5"},
            # `claude --model fable` alias verified live (2026-07-16)
            {"id": "fable", "label": "Fable 5"},
        ],
        # how a skill is invoked inside a prompt
        "skill_invoke": "/{name}",
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
        # codex's native skill mention (the mentions_v2 feature) — resolves a
        # discovered skill for real. `$name` only worked by the model choosing to
        # read the SKILL.md itself; `@` is the reliable trigger. Verified live 2026-07-16.
        "skill_invoke": "@{name}",
        "instructions_file": "AGENTS.md",
        "auth_file": ".codex/auth.json",
        "preset_session_id": False,
        "discover_session_id": _codex_discover_session_id,
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


def model_catalog(harness: str | None) -> list[dict]:
    """Selectable models for a harness as [{"id","label"}]: the built-in seed,
    replaced by woltspace.json's "catalog" when present. The catalog list IS the
    lever — add or hide a model by editing that one list, no code. Overlay entries
    may be bare id strings or {"id","label"} objects; labels fall back to the
    seed's (then to the id) so a user adding a model need only list its id.
    """
    seed = get_harness(harness).get("model_catalog", [])
    ov_catalog = _model_overlay(harness).get("catalog")
    if ov_catalog is None:
        return [dict(m) for m in seed]
    label_by_id = {m["id"]: m.get("label", m["id"]) for m in seed}
    out = []
    for item in ov_catalog:
        if isinstance(item, dict) and item.get("id"):
            out.append({"id": item["id"],
                        "label": item.get("label") or label_by_id.get(item["id"], item["id"])})
        elif isinstance(item, str):
            out.append({"id": item, "label": label_by_id.get(item, item)})
    return out


def is_valid_model(harness: str | None, model: str | None) -> bool:
    """True if `model` is usable for this harness.

    Freeform harnesses (freeform_model=True, e.g. opencode) accept ANY non-empty
    provider/model string — their catalog is suggestions, not a whitelist.
    Catalog-gated harnesses (claude, codex) require catalog membership.
    """
    if not model:
        return False
    if get_harness(harness).get("freeform_model"):
        return True
    return any(m["id"] == model for m in model_catalog(harness))


def tier_default_model(harness: str | None, tier: str | None) -> str | None:
    """The default model for a tier on this harness: woltspace.json's "tiers"
    override if present, else the built-in seed."""
    if not tier:
        return None
    overlay_tiers = _model_overlay(harness).get("tiers", {})
    if tier in overlay_tiers:
        return overlay_tiers[tier]
    return get_harness(harness)["models"].get(tier)


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
    out = []
    for hid, entry in HARNESSES.items():
        out.append({
            "id": hid,
            "label": entry.get("label", hid),
            "emoji": entry.get("emoji", ""),
            # per-tier default model (merged view — reflects woltspace.json overrides)
            "models": {tier: tier_default_model(hid, tier) for tier, _ in PICKER_TIERS},
            # full selectable list for the model picker (merged view)
            "catalog": model_catalog(hid),
        })
    return out


# --- Space-level default (woltspace.json "harness.default") ---------------
# The lodge default new sessions fall back to when a wolt has no override.
# Lives in woltspace.json (structured lodge settings), not .env.

def _woltspace_json_path() -> Path:
    return Path(os.environ.get("WOLTS_DIR", "/workspace/wolts")) / "woltspace.json"


def get_default_harness() -> str:
    """Read the lodge default harness. Falls back to the platform default."""
    try:
        cfg = json.loads(_woltspace_json_path().read_text())
        return resolve_harness(cfg.get("harness", {}).get("default"))
    except (json.JSONDecodeError, OSError, AttributeError):
        return DEFAULT_HARNESS


def set_default_harness(name: str) -> str:
    """Set the lodge default harness in woltspace.json. Returns the resolved value.

    Raises ValueError for an unknown harness — the caller (API) surfaces it.
    """
    if name not in HARNESSES:
        raise ValueError(f"unknown harness: {name}")
    path = _woltspace_json_path()
    try:
        cfg = json.loads(path.read_text()) if path.exists() else {}
    except (json.JSONDecodeError, OSError):
        cfg = {}
    cfg.setdefault("harness", {})["default"] = name
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    tmp.rename(path)
    return name


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
    include_launching: also count the launching shim (run-session.sh, uv, node)
        so a session that has not finished booting reads as alive.

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
