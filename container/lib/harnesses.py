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

# opencode session files are storage/session/<projectID>/<sessionID>.json,
# where the id looks like ses_<base62>. UNVERIFIED: the ses_ prefix and the
# storage layout are from the docs / sst-opencode source (id/id.ts) — confirm
# the exact filename shape live before trusting it.
_OPENCODE_SESSION_RE = re.compile(r"(ses_[0-9A-Za-z]+)\.json$")


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
    """Build an opencode CLI command line.

    UNVERIFIED (needs a live bench with jerpint before trusting):
      - Flags are from opencode.ai/docs (the TUI root command and `opencode run`
        share -m/--model, -s/--session, -c/--continue, --prompt). We launch the
        interactive TUI (attachable in tmux), NOT `opencode run` (headless), so
        the session behaves like claude/codex: `opencode --model <p/m> --prompt
        "<text>"`.
      - opencode can't preset a session id at spawn — like codex, it assigns its
        own `ses_...` id, so run-session.sh discovers it after launch (see
        _opencode_discover_session_id). Resume is `--session <id>`.
      - opencode's default permission mode is "allow all" (no trust/approval
        dialog like codex), so no bypass flag is needed. If a future version adds
        an approval gate, add `--agent` or a config preseed in wopencode rather
        than a flag here. `opencode run` also has `--auto`; the TUI root command
        may not — do NOT add it unrecognized.
      - Whether `--prompt` on the TUI auto-submits (vs pre-fills the composer) is
        unconfirmed. If it only pre-fills, the paste-settle transport still
        delivers the opening prompt; live-test to be sure.
    """
    wrapper = entry["wrapper"]
    if mode == "login":
        # `opencode auth login` is interactive (provider picker; Claude Pro/Max
        # and ChatGPT open a browser — not container-friendly). The seed flow in
        # wopencode is the real containerizable path; this is the manual fallback.
        return f"{wrapper} auth login"
    if mode not in ("spawn", "resume"):
        raise ValueError(f"unknown mode: {mode}")

    parts = [wrapper]
    if mode == "resume" and resume_id:
        parts += ["--session", resume_id]
    if model:
        parts += ["--model", model]
    if prompt:
        parts += ["--prompt", prompt]
    return " ".join(shlex.quote(p) for p in parts)


def _opencode_discover_session_id(data: dict, since: float) -> str | None:
    """Find the ses_ id opencode assigned to a just-spawned session.

    opencode writes session state to
    $XDG_DATA_HOME/opencode/storage/session/<projectID>/<sessionID>.json (default
    XDG_DATA_HOME is $HOME/.local/share). wopencode sets HOME to the wolt root, so
    the per-wolt path is <wolt>/.local/share/opencode/storage/session/. Returns
    the newest ses_ id created after `since`, preferring a file whose recorded
    directory matches the session dir (disambiguates concurrent sessions).

    UNVERIFIED: the storage path, the ses_ id shape, and the per-session JSON's
    directory field name are from docs/source, not a live run. Confirm before
    trusting resume under concurrency.
    """
    wolt = data.get("wolt", "")
    if not wolt:
        return None
    wolts_dir = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))
    storage_dir = (wolts_dir / wolt / ".local" / "share" / "opencode"
                   / "storage" / "session")
    if not storage_dir.exists():
        return None

    candidates = []
    for f in storage_dir.glob("**/ses_*.json"):
        try:
            if f.stat().st_mtime < since:
                continue
        except OSError:
            continue
        m = _OPENCODE_SESSION_RE.search(f.name)
        if m:
            candidates.append((f.stat().st_mtime, f, m.group(1)))
    if not candidates:
        return None
    candidates.sort(reverse=True)

    # Prefer a session whose recorded directory matches the session dir.
    # UNVERIFIED: field name — opencode session JSON has been seen to carry a
    # "directory" (and/or "cwd") key; try both, fall back to newest.
    session_dir = data.get("dir", "")
    if session_dir:
        for _, f, sid in candidates:
            try:
                meta = json.loads(f.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            cwd = meta.get("directory") or meta.get("cwd", "")
            if cwd == session_dir:
                return sid
    return candidates[0][2]


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
        # UNVERIFIED: model identifiers. opencode uses provider/model strings
        # from its models.dev catalog. Defaulting to the Anthropic provider so
        # the existing Claude Max subscription authorizes it (opencode supports
        # Claude Pro/Max OAuth). The exact ids drift — confirm the live set with
        # `opencode models` and edit these. Swapping provider (openrouter/*,
        # openai/*, opencode/* Zen, etc.) is a one-line change per tier.
        "models": {
            "raccoon": "anthropic/claude-opus-4-8",     # frontier / thinker
            "beaver": "anthropic/claude-sonnet-4-5",     # balanced / builder
            "otter": "anthropic/claude-haiku-4-5",       # fast / quick
            "rodent": "anthropic/claude-opus-4-8",       # legacy — treated as raccoon
            "wolf": "anthropic/claude-sonnet-4-5",
        },
        # Selectable models for the picker (new-main integration). UNVERIFIED ids
        # — same caveat as `models` above; edit after a live `opencode models`.
        "model_catalog": [
            {"id": "anthropic/claude-opus-4-8", "label": "Claude Opus 4.8"},
            {"id": "anthropic/claude-sonnet-4-5", "label": "Claude Sonnet 4.5"},
            {"id": "anthropic/claude-haiku-4-5", "label": "Claude Haiku 4.5"},
        ],
        # opencode mirrors Claude Code conventions (it even reads ~/.claude/skills
        # and CLAUDE.md). UNVERIFIED: whether a /skill mention in the opening
        # --prompt triggers the skill headlessly (same open question codex had).
        "skill_invoke": "/{name}",
        # opencode reads AGENTS.md as primary, CLAUDE.md as a documented
        # fallback. wopencode symlinks AGENTS.md -> CLAUDE.md for parity with
        # codex; the fallback means it would work even without the symlink.
        "instructions_file": "AGENTS.md",
        # auth.json lives under the data dir, not the config dir.
        "auth_file": ".local/share/opencode/auth.json",
        # opencode assigns its own ses_ id — discover after launch, like codex.
        "preset_session_id": False,
        "discover_session_id": _opencode_discover_session_id,
        # UNVERIFIED: opencode TUI paste/submit semantics. Start at codex's
        # proven 0.5s settle; live-bench and drop to 0.0 if it submits cleanly.
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
    """True if `model` is in this harness's (merged) selectable catalog."""
    if not model:
        return False
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


def session_has_agent_process(session_name: str, harness: str | None = None,
                              include_launching: bool = True) -> bool | None:
    """Check if a tmux session has a live agent process anywhere in its tree.

    Walks the full subtree from the pane's root PID — not just direct children.
    The actual tree is: pane(bash) → bash(run-session.sh) → bash(wrapper) → agent,
    so checking only direct children always misses the agent.

    harness=None matches ANY known harness's process names — for callers like
    the vulture that only see a tmux session and must not kill a live agent
    just because they can't tell which harness it runs.

    include_launching also counts run-session.sh itself as alive — a session
    that is still booting has no agent process yet but must not be reaped.

    Returns True/False, or None if the tmux session doesn't exist.
    """
    if harness is None:
        process_names = set().union(*(e["process_names"] for e in HARNESSES.values()))
    else:
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
