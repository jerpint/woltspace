# Environment variables

Every variable woltspace owns lives in the `WOLTSPACE_*` namespace. Everything
else on this page belongs to somebody else — a harness, a chat platform, a
cloud provider — and is consumed under the name that platform gave it.

Nothing in the platform reads an environment variable by hand. Reads go through
one helper, mirrored for the two import regimes:

- `src/woltspace/envvars.py` — the `woltspace` package
- `container/lib/env_compat.py` — the flat container runtime tree

```python
from env_compat import get_env          # or: from .envvars import get_env
wolts_dir = Path(get_env("WOLTSPACE_WOLTS_DIR", "/workspace/wolts"))
```

`get_env` prefers the canonical name and falls back to the pre-namespace one.
`export_both` fills in the other spelling of every renamed pair on its way to a
child process. Both are covered by `test/test_env_namespace.py`, which also
asserts the two copies of the helper have not drifted.

Shell and JavaScript do the same thing inline —
`${WOLTSPACE_WOLTS_DIR:-${WOLTS_DIR:-/workspace/wolts}}`,
`process.env.WOLTSPACE_WOLT_DIR || process.env.WOLT_DIR`.

**How thoroughly this is enforced.** For python, a test walks every module and
extensionless `container/bin/` script and fails on any direct read of a legacy
name — writes are exempt, since those are the both-name exports. Shell and
JavaScript get a weaker contract, because `$WOLT_NAME` in bash is the same
syntax whether the value came from the environment or from an assignment three
lines up, and `WOLT_NAME` in JS is the same token in `process.env.WOLT_NAME` as
in a `const`. Telling a read from a local would take dataflow analysis, so
instead those files must never name a legacy variable *alone*: the canonical
name has to appear in the same file, which is what the fallback shape and the
both-name exports already produce. A new script reading only the old spelling
fails; a new script reading the old spelling *next to* the new one does not.
Migration guides and `migrations/*.sh` are exempt — they are the record of what
a released version did, and rewriting them would falsify history.

---

## Legacy names

The unprefixed names below are still honoured everywhere, and everything that
spawns a child exports **both** spellings, so a wolt skill or tool written
against `$WOLT_NAME` keeps working untouched.

| canonical | legacy name still honoured |
|---|---|
| `WOLTSPACE_WOLTS_DIR` | `WOLTS_DIR` |
| `WOLTSPACE_WOLT_DIR` | `WOLT_DIR` |
| `WOLTSPACE_WOLT_NAME` | `WOLT_NAME` |
| `WOLTSPACE_WOLT_SESSION` | `WOLT_SESSION` |

**Sunset:** legacy names are honoured until 1.0, then removed. A process that
relies on one prints a single line to stderr at startup naming what to change:

```
deprecated env var WOLTS_DIR — use WOLTSPACE_WOLTS_DIR (legacy names honored until 1.0)
```

Once per process, not once per read, and only when the legacy name is doing
real work — a legacy name sitting beside its canonical twin (which is what
every both-name export produces) is silent.

**Where both names are exported:** `RuntimeLayout.apply_environment` in
`src/woltspace/layout.py`, the connector plans in `src/woltspace/channels.py`,
the control-plane launch in `src/woltspace/lifecycle.py`,
`build_environment` in `src/woltspace/container_entrypoint.py`,
`_SESSION_ENV_KEYS` and `_launch_command` in
`container/lib/session_runtime.py`, `switch_wolt` in `container/bot/core.py`,
`container/bin/run-session.sh`, and the `docker run` / `docker exec` argument
lists in the host `woltspace` launcher.

---

## The lodge and the wolt

| name | purpose | consumed where | default |
|---|---|---|---|
| `WOLTSPACE_WOLTS_DIR` | The data root — every wolt, plus `.space/` platform state. The one host mount in container mode. | `layout.py`, `container/lib/paths.py`, `sessions.py`, `wolts.py`, `sites.py`, `apps.py`, `harnesses.py`, `server/config.py`, the bot adapters, the creatures, the host launcher | `~/.woltspace/wolts` native, `/workspace/wolts` in the container |
| `WOLTSPACE_WOLT_DIR` | The active wolt's home directory. Where its `.env`, memory, site and sparks live. | `server/config.py`, `container/bot/core.py`, the bot adapters, `container/bin/gh-app-token`, `tui/src/tui-service.js`, `container/cron/digest.mjs` | the data root natively; `/workspace/wolt` in the container |
| `WOLTSPACE_WOLT_NAME` | The active wolt's name — the wolt this process belongs to. Also the sender identity a wolt posts under. Exported into every session, cron and worktree shell, so it is *never* an instruction about which wolt to create: see `WOLTSPACE_INIT_WOLT_NAME`. | `container_entrypoint.resolve_wolt_name`, `server/config.py`, `container/bot/core.py`, the bot adapters, `container/bin/notify`, `container/bin/woltspace`, the host launcher | unset (resolved from `woltspace.json`, else the first wolt on disk) |
| `WOLTSPACE_WOLT_SESSION` | Which session this process is. How `notify` and `push-view` route a message back to the chat that asked for it. | `container/bin/notify`, `container/bin/push-view`, `container/bin/woltspace` | unset (falls back to the tmux session name, then `main`) |
| `WOLTSPACE_WOLT_HOME` | The wolt home a harness wrapper should treat as `$HOME`. Set per session so credentials and harness config stay inside the wolt. | `container/bin/wclaude`, `wcodex`, `wopencode` | derived from `$PWD` under the data root |
| `WOLTSPACE_HOME_DIR` | The resolved `$HOME` a harness wrapper exports, with the XDG directories under it. | `container/bin/wclaude`, `wcodex`, `wopencode` | derived from `WOLTSPACE_WOLT_HOME` |
| `WOLTSPACE_WORKDIR` | The working directory a session was spawned in. | exported by `container/bin/run-session.sh` for the session's own tools | the session's registry `dir` |

## The install and the control plane

| name | purpose | consumed where | default |
|---|---|---|---|
| `WOLTSPACE_DIR` | The install root — the source checkout or the wheel's `_bundle`. A value naming a directory that is no longer an install is ignored in favour of the one actually running. | `layout.resolve_install_root`, `server/config.py`, `container/lib/session_runtime.py`, the host launcher | the running install |
| `WOLTSPACE_HOST` | Bind address for the control plane. Rewritten into a dialable form for anything a child connects to. | `layout.py`, `container/creatures/wolf.py` | `127.0.0.1` |
| `WOLTSPACE_PORT` | Control-plane port. `PORT` is honoured as a second-choice source. | `layout.py`, the host launcher's port publish | `7777` |
| `WOLTSPACE_API` | The full endpoint of the control plane that owns this process. The one address every child is told, so a second instance talks to itself rather than to the first. | `container/bin/notify`, `push-view`, `container/bin/woltspace`, `tui/src/api.js` | stamped from host + port |
| `WOLTSPACE_PYTHON` | The exact Python interpreter running the control plane. Platform helpers and sessions inherit it so an older ambient macOS `python3` cannot load platform code. | `layout.py`, `session_runtime.py`, `container/bin/session-reg`, `container/bin/woltspace-python` | `sys.executable` |
| `WOLTSPACE_DEFAULT_HARNESS` | Installer hint for a fresh lodge's first harness choice. Any registered harness is accepted; it never overrides a choice already recorded. | `server/state.py`, `server/app.py`, `doctor.py` | unset (show the first-run question) |
| `WOLTSPACE_STARTER_SEED` | Local colony-seed directory or Git URL to install once when an empty lodge completes its first harness choice. Set `none` to disable the starter. Installation needs network access when the source is remote; failure is recorded without blocking onboarding. | `server/state.py`, `woltspace.seed.install_seed` | latest `main` of `jerpint/woltspace-starter-lodge` (unpinned) |
| `WOLTSPACE_LODGE_AUTH` | Opt-in lodge auth. When truthy, every lodge request needs a lodge key (`X-Woltspace-Key` or `Authorization: Bearer`) or a browser paired with `woltspace pair`. Keys live in `<data root>/.space/run/lodge/` (0600), never on a command line: the owner key for the terminal and the lodge itself, the wolt key for tools inside a wolt session, which can't grant auto mode, publish an app or change lodge settings. App hosts are not gated. | `server/app.py`, `container/lib/lodge_auth.py` | unset (off) |
| `WOLTSPACE_LODGE_KEY` | A lodge key for one script, ahead of the key files: set it in that script's shell only, never in `.env`, or every wolt session inherits it. Never passed to apps. | `container/lib/lodge_auth.py`, `tui/src/api.js` | unset |
| `WOLTSPACE_URL` | Explicit API base for the terminal cockpit, ahead of `WOLTSPACE_API`. | `tui/src/api.js` | unset |
| `WOLTSPACE_ISOLATION` | `host` or `external` — whether this runtime owns its own home, and which harness-launch path applies. | `layout.py`, `container/lib/runtime_context.py`, `server/app.py`, the harness wrappers | `host` natively, `external` in the container |
| `WOLTSPACE_ENTRYPOINT` | Truthy only in the process launched as the platform entrypoint. A guest — a stray `serve`, a smoke test, an agent poking around — must not claim the data root, the tunnel, or the bot token. | `layout.is_entrypoint`, every connector plan in `channels.py` | unset (guest) |
| `WOLTSPACE_INSTANCE_ID` | Identifies one control plane, so tunnel state and ownership records can say who wrote them. | `supervisor.py`, `server/app.py`, `server/tunnel.py` | assigned per start |
| `WOLTSPACE_ALLOW_SHARED_DATA_ROOT` | Opt out of the refusal to start a second control plane on a data root another one already owns. | `doctor.py` | unset (refuse) |
| `WOLTSPACE_CONFIG` | Path override for `woltspace.json`. | `src/woltspace/config.py` | `<data root>/woltspace.json` |
| `WOLTSPACE_BACKUP_AGE_RECIPIENT` | An age public key. When set, `woltspace backup` writes one `.tar.gz.age` encrypted to it, `.env` files and `config.json` inside; `age` must be on `PATH` or the backup fails. See `docs/backup.md`. | `src/woltspace/backup.py` | unset (plain archive, secrets withheld) |

## Connectors and surfaces

| name | purpose | consumed where | default |
|---|---|---|---|
| `WOLTSPACE_PUBLIC_TUNNEL` | Whether to run a tunnel at all. With a Cloudflare token and URL it runs the named, Access-gated tunnel; with neither it runs a throwaway public URL. | `server/tunnel.py`, `supervisor.py`, `container_entrypoint.py` | `true` in the container, `false` natively |
| `WOLTSPACE_TUI_BIN` | Path to the terminal cockpit binary. | `src/woltspace/tui.py` | resolved from `PATH` |
| `WOLTSPACE_WOLF` | Enable or disable the cron scheduler connector. | `channels.py` | enabled for the entrypoint |
| `WOLTSPACE_VULTURE` | Enable or disable the session reaper connector (closes idle sessions, cleans up dead ones). | `channels.py` | enabled for the entrypoint |
| `WOLTSPACE_SHARING_ENABLED` | Whether apps may be given public tunnels. | `container/lib/apps.py` | on |
| `WOLTSPACE_USER` | Display name for the human in the cockpit. `HUMAN_NAME` is honoured as a second-choice source. | `tui/src/ui/App.js` | the OS username |
| `WOLTSPACE_TMUX_BIN` | `tmux` binary, for a host that keeps it somewhere unusual. | `container/lib/runtime_context.py`, `session_runtime.py` | `tmux` |
| `WOLTSPACE_PS_BIN` | `ps` binary, same reason. | `container/lib/runtime_context.py`, `container/lib/tunnel.py` | `ps` |

## Host launcher and image build

These are read by the host `woltspace` bash launcher and the Dockerfile — not
by the platform runtime.

| name | purpose | consumed where | default |
|---|---|---|---|
| `WOLTSPACE_CONTAINER` | Container name to operate on, for running more than one lodge. | the host launcher, `tui/src/attach.js` | `woltspace`, hash-suffixed for a custom data root |
| `WOLTSPACE_CONTAINER_HOME` | The per-wolt `$HOME` the container image builds. Containers are the only isolation mode that owns a home outright, so harness credentials sit at a fixed path rather than wherever `$HOME` points. | `server/config.py` | `/home/node` |
| `WOLTSPACE_IMAGE` | Docker image to run. The desktop app pulls and runs it. The host launcher builds and tags the image locally under this name, and inspects and runs that tag; an exported published name such as `woltspace/woltspace:latest` therefore gets a local build under it. A second lodge on one machine sets both `WOLTSPACE_CONTAINER` and `WOLTSPACE_IMAGE`, or a build for one lodge retags the other's image. For the launcher, one image reference (a letter or digit, then letters, digits, `.` `_` `-` `:` `/` `@`); read from the launching shell only. | `desktop/src-tauri/src/docker.rs`, the host launcher | the published image (desktop), `woltspace` (launcher) |
| `WOLTSPACE_PUBLISH_HOST` | Host address the container's port is published on, e.g. `127.0.0.1` to keep the lodge off the local network (unset, docker publishes on every interface, past most host firewalls). Read from the launching shell only. | the host launcher | unset (all interfaces) |
| `WOLTSPACE_DOCKER_NETWORK` | Docker network the container joins, e.g. one whose only way out is an egress proxy (pair it with `HTTPS_PROXY`/`NO_PROXY` in the container's env file). One network name, not `host` or `none`; read from the launching shell only. | the host launcher | unset (docker's default bridge) |
| `WOLTSPACE_ENV_FILE` | The env file the launcher seeds and passes to `docker run --env-file`. A path outside the data root keeps it out of the container's reach (the container's first process runs as root with that environment). The lodge lets the process environment win over the data root's `.env`, so it needs no copy there. `init` seeds it from `.env.example` only when it is missing, and turns its commented `# WOLTSPACE_PUBLIC_TUNNEL=false` line, if there is one, into the public-link answer (`false` when non-interactive), in a file you made too. A file made before the lodge does not make `init` treat an empty data root as an existing lodge. Read from the launching shell only. | the host launcher | `<data root>/.env` |
| `WOLTSPACE_LOCAL` | Sticky equivalent of `--local`: build the image from this checkout. | the host launcher | `false` |
| `WOLTSPACE_NONINTERACTIVE` | Run `init` without prompting. Pair with `WOLTSPACE_INIT_WOLT_NAME` to name the first wolt. | the host launcher | unset |
| `WOLTSPACE_INIT_WOLT_NAME` | Which wolt a fresh non-interactive `init` should **create**. A different question from `WOLTSPACE_WOLT_NAME`, which names the wolt a shell already belongs to — see the note below. | the host launcher | unset (the lodge asks) |
| `WOLTSPACE_BRANCH` | Build arg naming the branch the image installs from. | `container/Dockerfile` | `main` |
| `WOLTSPACE_PYPI_VERSION` | Build arg: the `woltspace` version the image installs. | `container/Dockerfile` | the release being built |
| `WOLTSPACE_TUI_VERSION` | Build arg: the `@woltspace/tui` version the image installs. The tui declares the minimum `woltspace` version it needs; woltspace does not check the tui's version, so the two are installed and upgraded on their own. | `container/Dockerfile` | `latest` |
| `WOLTSPACE_NODE_SUDO` | Build arg: `false` builds an image where `node` cannot become root (no sudoers rule, no `sudo`). A session can then no longer `apt-get install`, nor install into the root-owned global npm/uv dirs; bake such tools into the image. The launcher passes it through from the launching shell on every build, so set it for every `build` and `rebuild`. The image records the value as the label `woltspace.node_sudo`; when it says `false`, the launcher runs the container with `--security-opt no-new-privileges`, so no setuid binary can raise a process's privileges either. | `container/Dockerfile`, the host launcher | `true` |
| `WOLTSPACE_CLAUDE_MANAGED_SETTINGS` | Build arg: a path inside the build context (the checkout for `--local`, `container/` otherwise) installed as `/etc/claude-code/managed-settings.json`, root-owned 0644: the Claude Code settings no user or project file can override. Must be valid JSON. Passed through from the launching shell on every build. | `container/Dockerfile`, the host launcher | unset (none) |
| `HOST_UID` / `HOST_GID` | The host user the container's `node` user is matched to, so files a wolt writes are readable on the host. | `container_entrypoint.run_root_phase` | the invoking user |

### A second, hardened lodge

The launcher's options only add up to a locked-down lodge when they are set
together. They are read from the launching shell only, so keep them in a small
script you run for that lodge, never in its data root's `.env`:

```bash
export WOLTSPACE_WOLTS_DIR=~/lodge-b
export WOLTSPACE_CONTAINER=lodge-b
export WOLTSPACE_IMAGE=lodge-b
export WOLTSPACE_PORT=7778
export WOLTSPACE_NODE_SUDO=false
export WOLTSPACE_CLAUDE_MANAGED_SETTINGS=managed-settings.json
export WOLTSPACE_ENV_FILE=~/lodge-b.env
export WOLTSPACE_PUBLISH_HOST=127.0.0.1
export WOLTSPACE_DOCKER_NETWORK=lodge-b-egress
woltspace init
```

- `WOLTSPACE_WOLTS_DIR`: its own data root, so the two lodges share no wolts or state.
- `WOLTSPACE_CONTAINER`: its own container, so `start` and `stop` never touch the other lodge's.
- `WOLTSPACE_IMAGE`: its own image, so a build for either lodge never retags the other's.
- `WOLTSPACE_PORT`: its own host port, since the other lodge holds 7777.
- `WOLTSPACE_NODE_SUDO=false`: `node` cannot become root, and the launcher adds `no-new-privileges`.
- `WOLTSPACE_CLAUDE_MANAGED_SETTINGS`: Claude Code settings no session can override (a JSON file in the build context).
- `WOLTSPACE_ENV_FILE`: outside the data root, so a session cannot write variables the root phase receives at the next create.
- `WOLTSPACE_PUBLISH_HOST=127.0.0.1`: the port is reachable from this machine only.
- `WOLTSPACE_DOCKER_NETWORK`: a network you made whose only way out is your egress proxy (`HTTPS_PROXY` in the env file).

### Creating a wolt vs. being one

`WOLTSPACE_INIT_WOLT_NAME` and `WOLTSPACE_WOLT_NAME` look like the same
question and are not. The first says *create a wolt with this name*; the second
says *this process belongs to that wolt*, and it reaches every session, cron and
worktree shell. A scripted install launched from inside a wolt would otherwise
read the ambient value and silently name the new wolt after the current one.

So the launcher resolves the first wolt's name in this order:

1. `WOLTSPACE_INIT_WOLT_NAME`, if set.
2. otherwise `WOLTSPACE_WOLT_NAME` — deprecated for this purpose, honoured for
   install scripts that predate the split, and reported on stderr.
3. except when `WOLTSPACE_WOLT_SESSION` is also set, which means the value
   arrived ambiently from a wolt session. Then it is ignored and the reason is
   printed, and the lodge asks for a name the usual way.

The deprecated step 2 goes away at 1.0 with the rest of the legacy names.

## Tests

| name | purpose | consumed where | default |
|---|---|---|---|
| `WOLTSPACE_TEST_LIVE_SEND` | Opt in to tests that talk to the live bot and reach a real phone. | `test/conftest.py` | off |
| `WOLTSPACE_TEST_REAL_SPAWN` | Opt in to tests that boot a real agent process. | `test/conftest.py` | off |
| `TEST_CHAT_ID` | The dedicated test group chat. Never discovered, only configured. | `test/conftest.py` | unset |
| `TEST_VERBOSE` | Post per-test results to the test group. | `test/run-tests.sh` | on |

---

## Time zone

The lodge runs on the container's local time, and so does everything that
reads the clock: the wolf fires `0 9 * * *` at 09:00 on that clock, and
`woltspace wolf list` shows that zone in its header and every next run in it.
The image sets no zone, so a container lodge runs on UTC by default. A native
lodge uses its machine's own zone.

To run on a named zone instead, put the standard `TZ` variable with an IANA
name in the container's env file (`WOLTSPACE_ENV_FILE`, by default
`<data root>/.env`); the image ships the zone database:

```bash
# any IANA name, e.g. Asia/Tokyo
TZ=Europe/Berlin
```

A named zone follows its daylight-saving changes, so a cron keeps its local
hour across them without edits; DST gaps and repeated hours still need review.
UTC never shifts, which suits schedules tied to a global time. A fixed offset
such as `Etc/GMT-2` never changes for DST either.

The env file is read only when the container is created. Recreate it with
`woltspace stop` then `woltspace start` (or `woltspace rebuild`); `start` on a
stopped container restarts it with its old environment, and setting `TZ` in
a shell changes nothing for the running lodge. Then check the zone and the
next runs:

```bash
woltspace wolf list    # header: "lodge time · Europe/Berlin"
```

**Moving an existing lodge.** Nothing rewrites schedules for you; review them
together before you recreate the container:

- **Recurring crons** keep their hours on the new clock. An hour that was
  converted from local time to UTC by hand goes back to the local hour.
- **One-offs** (`at`) without an offset are wall-clock times, read in the new
  zone: each moves by the zone difference, and one already past fires as soon
  as the wolf starts. An `at` with an explicit offset keeps its moment.
- **Manual DST reminders** (crons that tell you to shift the others twice a
  year) are no longer needed once the zone is named; remove them.
- **Catch-up.** Last-run stamps are wall-clock minutes without a zone, and
  they are kept. On the first start in the new zone, a cron that already ran
  that day can match its last 24 hours again and fire once more. Set
  `"catch_up": false` on the crons where a repeat matters, and put it back after
  the first day if you want catch-up.

Per-cron zones do not exist; one zone serves the whole lodge.

## Variables we consume but do not own

These keep the names their own platform gave them, and are configured in
`<data root>/.env`.

**Chat platforms** — `ENABLE_TELEGRAM_BOT`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_ALLOWED_USERS`, `TELEGRAM_CHAT_ID`, `ENABLE_SLACK_BOT`,
`SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_OWNER_USER`,
`SLACK_NOTIFY_CHANNEL`. Read by
`src/woltspace/channels.py` when planning connectors, by the Telegram
connector (`container/connectors/telegram/`) and by the Slack adapter in
`container/bot/`. A customized Telegram is a connector folder of its own under
`<data root>/.space/connectors/<name>/` (see `docs/connectors.md`). Slack
always uses the platform's owner-DM Agent View adapter.
`TELEGRAM_ALLOWED_USERS` and `SLACK_NOTIFY_CHANNEL` also name the chats a
wolt may send a file, a voice note or a spoken message to, beside the chat
its session was started from (`server/notify.py`). With no
`SLACK_NOTIFY_CHANNEL`, a file reaches Slack only from a session started in
Slack. Text is not held to these lists.
`BOT_ADAPTER` names which adapter a bot process is.

**Models** — `LLM_MODEL` picks the bot brain's model;
`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY` are passed through
to litellm and the image generator.

**Voice** — `OPENAI_API_KEY` is also the key that transcribes a voice note the
human sends (by default: `woltspace.json` `transcription`, set with
`woltspace transcription set`, picks any OpenAI-style endpoint, its model and
the variable holding its key) and speaks a message a wolt sends with
`notify --speak`.
The spoken message goes to `woltspace.json` `speech`, set with
`woltspace speech set`: any OpenAI-style `/audio/speech` endpoint (OpenAI by
default, or a speech model on this machine over `http://127.0.0.1`), its
model, its voice and the variable holding its key (`--no-key` for a local
server). `OPENAI_TTS_MODEL` (default `gpt-4o-mini-tts`) and `OPENAI_TTS_VOICE`
(default `alloy`) still pick the model and the voice when `speech` leaves them
out. Read by `server/notify.py` on every spoken message; with OpenAI, each one
is an API request billed to that key.
A wolt may speak in its own voice: `"speech": {"voice": "coral", "instructions":
"Speak slowly."}` in its `wolt/wolt.json`, both keys optional. The wolt is the
sending session's; its voice comes before the lodge's, and `instructions` (at
most 500 characters, sent only when set) is a hint on how to speak. Server,
model and key stay the lodge's. The voice must be one the lodge's speech server
knows (`coral` is OpenAI's); one it refuses sends the message as text. The
hint goes to OpenAI only.

**Harnesses** — `CLAUDE_CONFIG_DIR`, `CLAUDE_CODE_OAUTH_TOKEN`,
`CLAUDE_CODE_DISABLE_AUTO_MEMORY`, `CODEX_HOME`, `OPENCODE_CONFIG_DIR` and the
`XDG_*` directories. Located by `container/lib/harness_auth.py`, carried into
sessions by `_SESSION_ENV_KEYS` in `container/lib/session_runtime.py`, and set
per wolt by the `container/bin/w*` wrappers.

**Cloudflare** — `CLOUDFLARE_TUNNEL_TOKEN` and `CLOUDFLARE_TUNNEL_URL` run a
named, Access-gated tunnel; `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID` and
`CLOUDFLARE_ZONE_ID` are used by the `cloudflare` skill to manage it. Consumed
in `server/tunnel.py` and `container/lib/tunnel.py`.

**GitHub** — `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`,
`GITHUB_APP_PRIVATE_KEY`, minted into a short-lived token by
`container/bin/gh-app-token`.

**Spotify** — `SPOTIFY_ID`, `SPOTIFY_SECRET`, `SPOTIFY_USER`,
`SPOTIFY_REFRESH_TOKEN`, `SPOTIFY_ACCESS_TOKEN`, for the digest's playlist
curation in `container/cron/digest.mjs`.

**Ambient** — `HOME`, `PATH`, `PORT`, `TUI_PORT`, `HUMAN_NAME`, `DEV_MODE`,
`LANG`.
