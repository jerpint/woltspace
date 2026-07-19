# Adding a Harness

A **harness** is the CLI coding agent a session runs on — Claude Code, Codex, and opencode today. This guide is for contributors bringing their own. If you can drive an agent from a terminal, keep per-agent state under a directory, and resume a past conversation, it can be a woltspace harness.

## The mental model

Three ideas, kept deliberately separate:

- **Harness = the engine.** The actual CLI binary and how you spell its flags. This is the only thing that varies between agents.
- **Creature / tier = the lore.** `raccoon` (thinker), `beaver` (builder), `otter` (quick) are *relative* capability tiers, not model names. A wolt keeps its creature identity no matter which harness it runs on.
- **Model = `resolve(harness, tier)`.** Each harness maps the tiers to its own model identifiers. `otter` is "this harness's fast model" — `haiku` on Claude, a small model on opencode, and so on.

The load-bearing rule: **sessions are born with a harness and resume on it, for life.** Conversation state doesn't transfer between engines, so a session started on Codex can never be revived by Claude. A wolt can switch its *default* harness for new sessions; old sessions keep theirs. The bridge across a switch is `wolt/memory/` — plain markdown, engine-agnostic by design. Same creature, same memories, different brain stem.

Everything engine-specific lives in one place: `container/lib/harnesses.py`. Adding a harness is adding one entry to the `HARNESSES` table plus a wrapper script. Nothing else in the platform should need to learn how your agent spells its flags — if you find yourself editing `sessions.py`, `run-session.sh`, or the bot to special-case a harness, the abstraction is leaking and the fix belongs in the table.

## The harness entry contract

Each `HARNESSES` entry is a dict. Fields, with what they mean and how `claude` vs `codex` answer them:

| Field | Meaning |
|---|---|
| `wrapper` | Absolute path to the per-wolt wrapper script (see below). Resolved relative to `harnesses.py` so the dev clone drives its own `bin/`. |
| `command` | A builder function `(entry, mode, **kwargs) -> str` that returns the full shell command line. `mode` is `spawn`, `resume`, or `login`. **The only place your CLI's flag syntax exists.** |
| `label` / `emoji` | Display metadata for pickers and badges, exposed through the API. `claude`: "Claude Code" 🟠; `codex`: "Codex" ⬛. |
| `process_names` | Set of `comm` names that mean "the agent is live" in a session's process tree. Used by liveness checks and the vulture. Get this wrong and the vulture reaps live sessions. `{"claude"}`, `{"codex"}`. |
| `models` | Creature tier → model flag value. Keys: `raccoon`, `beaver`, `otter`, plus legacy aliases `rodent` (→ treat as raccoon) and `wolf` (→ balanced). `claude`: `opus`/`sonnet`/`haiku`. |
| `skill_invoke` | Format string for invoking a skill inside a prompt. `claude`: `/{name}`; `codex`: `${name}`. Used to inject the start-chat skill into the opening prompt. |
| `instructions_file` | The project-instructions filename the agent reads. `claude`: `CLAUDE.md`; `codex`: `AGENTS.md`. The wrapper symlinks it to the wolt's `CLAUDE.md`. |
| `auth_file` | Path (relative to the per-wolt HOME) where credentials live. `claude`: `.claude/.credentials.json`; `codex`: `.codex/auth.json`. Used by boot auth checks. |
| `preset_session_id` | `True` if the CLI accepts a session id *you* generate at spawn (`claude --session-id <uuid>`). `False` if it assigns its own (`codex`, `opencode`). |
| `discover_session_id` | `None` when `preset_session_id` is `True`. Otherwise a function `(session_data, since) -> str | None` that finds the id the agent assigned, by watching where it writes session state on disk (see the session-id story). |
| `paste_settle` | Seconds to wait between pasting a message and sending Enter, for human-attachable TUIs. `claude`: `0.0`; `codex`: `0.5` (its TUI folds an immediate Enter into the paste). Tune this live — it breaks silently. |

`build_command(harness, mode, **kwargs)` dispatches to your `command` builder; `creature_model(harness, tier)` reads `models`; `session_has_agent_process(...)` reads `process_names`. Study `_claude_command` (preset id, simple flags) and `_codex_command` (discovered id, approval-bypass flag) as the two shapes to copy from.

## The wrapper script contract

The command builder points at a wrapper (`wclaude`, `wcodex`, …) instead of the bare binary. The wrapper is what makes a shared container image behave per-wolt. Every wrapper does the same jobs — read one and copy it:

- **Per-wolt HOME.** Derive the wolt root from `$PWD` (`/workspace/wolts/<name>/…`) and `export HOME` to it, so the agent's config, sessions, and history land under that wolt and nowhere else. Point whatever env vars the agent uses for its config/data/state dirs at the wolt too (`CODEX_HOME`; opencode uses `XDG_DATA_HOME`/`XDG_CONFIG_HOME`).
- **Auth self-heal from a shared seed.** Credentials are copied — never symlinked — from a shared seed in the mount (`wolts/.codex/auth.json`, `wolts/.claude/.credentials.json`) into the per-wolt location on first run. Symlinks break because agents rewrite the file atomically on token refresh (see gotchas).
- **Instructions symlink.** Symlink the agent's instructions filename to the wolt's `CLAUDE.md` so one source of truth survives a harness switch.
- **Skills.** Wire the agent's skill directory to the wolt's synced `.claude/skills` (codex symlinks `.agents/skills`; opencode reads `~/.claude/skills` natively, so nothing is needed). The platform re-syncs skills every boot.
- **Config / trust preseed.** If the agent shows a blocking trust/approval dialog on first launch in a directory, preseed its config to pre-trust the working dir so a headless spawn doesn't hang (see gotchas).

Then `exec <binary> "$@"`. Mark the script executable and drop it in `container/bin/`.

## The session-id story: preset vs discover

Resume needs a stable id. Two patterns:

**Preset (claude).** The CLI takes an id you generate. Set `preset_session_id: True`; `prepare_session_command` mints a UUID at spawn, stamps it on the session as `harness_session_id`, and passes it via your spawn flag. Resume just replays it. `discover_session_id` is `None`. This is the easy path — use it if your CLI supports it.

**Discover (codex, opencode).** The CLI assigns its own id and writes it to disk; you can't preset one. Set `preset_session_id: False` and provide a `discover_session_id(session_data, since)` function. After spawn, `run-session.sh` fires `session-reg discover-id` in the background, which polls your function until the id appears, then stamps `harness_session_id` so resume works.

To implement `discover_session_id`: find where the agent writes new session/rollout files (codex: `$CODEX_HOME/sessions/**/rollout-*.jsonl`; opencode: `$XDG_DATA_HOME/opencode/storage/session/**/ses_*.json`), glob for files modified after `since`, extract the id from the filename with a regex, and return the newest. Under concurrent sessions of the same wolt, disambiguate by reading each file's recorded working directory and preferring the one that matches `session_data["dir"]`. Fall back to newest-by-mtime. Copy `_codex_discover_session_id` — it does exactly this.

## Gotchas we learned the hard way

- **Trust / approval dialogs block headless spawns.** Codex shows a trust-this-directory prompt even with its sandbox-bypass flag; unhandled, it hangs a spawned session forever. Preseed the agent's config to trust the wolt dir (codex writes `[projects."<dir>"] trust_level = "trusted"` into `config.toml`). Check for this the first time you bench a new agent — it won't show up in tests.
- **Login-shell PATH stripping.** If the agent runs model commands through a login shell (`bash -lc`), the shell rebuilds `PATH` from `/etc/profile` and drops the woltspace bins, so `notify`/`push-view`/`session-reg` become "command not found". Two fixes are already in place: `/etc/profile.d/woltspace-path.sh` restores the bins for every login shell, and codex additionally sets `shell_environment_policy inherit = "all"` in its config. If your agent uses a curated environment, set its equivalent in the wrapper.
- **`paste_settle` timing.** The only universal transport for human-attachable sessions is the tmux paste dance: set-buffer + paste-buffer, then a separate Enter keystroke. Some TUIs fold an Enter that arrives too fast into the paste and never submit. Bench the paste-then-submit flow live and set `paste_settle` accordingly — this fails silently, never in a unit test.
- **Instructions file + skills symlinks.** Agents disagree on the instructions filename (`CLAUDE.md` vs `AGENTS.md`) and where skills live. Symlink the agent's expectations onto the wolt's already-synced files so a wolt keeps its instructions and skills across a harness switch, for free.
- **Auth refresh copies go stale.** Auth files with rotating refresh tokens are the subtle one. Copying a seed once per wolt can drift if the agent refreshes tokens and the copies diverge — you may end up wanting a single shared auth dir instead of per-wolt copies. Decide this during live auth testing; it's not visible offline.
- **Model identifiers drift.** Whatever you put in `models` will bit-rot as the provider renames models. Mark them clearly and confirm the live set with the agent's own model-listing command rather than trusting docs.

## Checklist: adding harness X

1. **Research the CLI.** Install method suitable for a Docker image (npm / curl / binary) and the binary name. Non-interactive/attachable invocation, the model-selection flag, and how to pass an opening prompt. The session model: does it preset or assign ids, and where on disk? Auth: how to log in, where credentials land, what's containerizable. Config: file location and format, any trust/approval dialog. Instructions file and skills support.
2. **Add the `HARNESSES` entry** in `container/lib/harnesses.py` — a `_x_command` builder, the fields above, and a `_x_discover_session_id` if it assigns its own ids. Mark anything you couldn't verify live with a clear `# UNVERIFIED:` comment.
3. **Write `container/bin/wx`** modeled on `wcodex`/`wclaude` (per-wolt HOME + config dirs, auth self-heal, instructions symlink, skills, config/trust preseed). `chmod +x` it.
4. **Add the Dockerfile install** — a build stage (for independent layer caching) or an inline `RUN`, matching how codex is installed, plus any ENV the agent needs.
5. **Test.**
   - Offline: `uv run --extra test pytest test/ -k harness` — confirms the table is well-formed and the builders produce sane command lines.
   - Live bench: in a scratch tmux pane inside a running container, drive the wrapper by hand — `spawn`, attach, paste a message, confirm submit, let it write a session file, then `resume` and confirm it picks up the same conversation. This is where trust dialogs, paste timing, and session-id discovery reveal themselves.
   - Then rebuild the image from your branch and run a real session end to end (auth, spawn, resume) with the platform owner. Auth token-refresh staleness only shows up here.
