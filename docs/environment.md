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
| `WOLTSPACE_API` | The full endpoint of the control plane that owns this process. The one address every child is told, so a second instance talks to itself rather than to the first. | `container/bin/notify`, `push-view`, `send-to-session`, `container/bin/woltspace`, `tui/src/api.js` | stamped from host + port |
| `WOLTSPACE_URL` | Explicit API base for the terminal cockpit, ahead of `WOLTSPACE_API`. | `tui/src/api.js` | unset |
| `WOLTSPACE_ISOLATION` | `host` or `external` — whether this runtime owns its own home, and which harness-launch path applies. | `layout.py`, `container/lib/runtime_context.py`, `server/app.py`, the harness wrappers | `host` natively, `external` in the container |
| `WOLTSPACE_ENTRYPOINT` | Truthy only in the process launched as the platform entrypoint. A guest — a stray `serve`, a smoke test, an agent poking around — must not claim the data root, the tunnel, or the bot token. | `layout.is_entrypoint`, every connector plan in `channels.py` | unset (guest) |
| `WOLTSPACE_INSTANCE_ID` | Identifies one control plane, so tunnel state and ownership records can say who wrote them. | `supervisor.py`, `server/app.py`, `server/tunnel.py` | assigned per start |
| `WOLTSPACE_ALLOW_SHARED_DATA_ROOT` | Opt out of the refusal to start a second control plane on a data root another one already owns. | `doctor.py` | unset (refuse) |
| `WOLTSPACE_CONFIG` | Path override for `woltspace.json`. | `src/woltspace/config.py` | `<data root>/woltspace.json` |

## Connectors and surfaces

| name | purpose | consumed where | default |
|---|---|---|---|
| `WOLTSPACE_PUBLIC_TUNNEL` | Whether to run a tunnel at all. With a Cloudflare token and URL it runs the named, Access-gated tunnel; with neither it runs a throwaway public URL. | `server/tunnel.py`, `supervisor.py`, `container_entrypoint.py` | `true` in the container, `false` natively |
| `WOLTSPACE_TUI_BRIDGE` | Enable or disable the pty bridge connector. | `channels.py` | enabled for the entrypoint |
| `WOLTSPACE_TUI_PORT` | Port for the pty bridge. `TUI_PORT` is honoured as a second-choice source. | `channels.py` | API port + 1 |
| `WOLTSPACE_TUI_SERVICE_BIN` | Path to the `woltspace-tui-service` binary, when it is not on `PATH`. | `channels.py` | resolved from `PATH` |
| `WOLTSPACE_TUI_BIN` | Path to the terminal cockpit binary. | `src/woltspace/tui.py` | resolved from `PATH` |
| `WOLTSPACE_WOLF` | Enable or disable the cron scheduler connector. | `channels.py` | enabled for the entrypoint |
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
| `WOLTSPACE_IMAGE` | Docker image to run. | `desktop/src-tauri/src/docker.rs` | the published image |
| `WOLTSPACE_LOCAL` | Sticky equivalent of `--local`: build the image from this checkout. | the host launcher | `false` |
| `WOLTSPACE_NONINTERACTIVE` | Run `init` without prompting. Pair with `WOLTSPACE_INIT_WOLT_NAME` to name the first wolt. | the host launcher | unset |
| `WOLTSPACE_INIT_WOLT_NAME` | Which wolt a fresh non-interactive `init` should **create**. A different question from `WOLTSPACE_WOLT_NAME`, which names the wolt a shell already belongs to — see the note below. | the host launcher | unset (the lodge asks) |
| `WOLTSPACE_BRANCH` | Build arg naming the branch the image installs from. | `container/Dockerfile` | `main` |
| `WOLTSPACE_PYPI_VERSION` | Build arg: the `woltspace` version the image installs. | `container/Dockerfile` | the release being built |
| `WOLTSPACE_TUI_VERSION` | Build arg: the `@woltspace/tui` version the image installs. The tui declares the minimum `woltspace` version it needs; woltspace does not check the tui's version, so the two are installed and upgraded on their own. | `container/Dockerfile` | `latest` |
| `HOST_UID` / `HOST_GID` | The host user the container's `node` user is matched to, so files a wolt writes are readable on the host. | `container_entrypoint.run_root_phase` | the invoking user |

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

## Variables we consume but do not own

These keep the names their own platform gave them, and are configured in
`<data root>/.env`.

**Chat platforms** — `ENABLE_TELEGRAM_BOT`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_ALLOWED_USERS`, `TELEGRAM_CHAT_ID`, `ENABLE_SLACK_BOT`,
`SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_NOTIFY_CHANNEL`. Read by
`src/woltspace/channels.py` when planning connectors and by the adapters in
`container/bot/`. `TELEGRAM_BOT_DIR` / `TELEGRAM_BOT_MODULE` and the `SLACK_*`
equivalents point a connector at the adapter to run, and are derived by
`container_entrypoint.build_environment` rather than set by hand.
`BOT_ADAPTER` names which adapter a bot process is.

**Models** — `LLM_MODEL` picks the bot brain's model;
`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY` are passed through
to litellm and the image generator.

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
