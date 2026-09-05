# Native and container

Woltspace is one product with one codebase. Native and container are not two
implementations of it — they are two answers to a single question:

> **Whose machine takes the risk when a wolt runs a command?**

- **Native** — the control plane runs on your machine, in your shell, with the
  harness you already logged into. A wolt working in one of your repos touches
  your files, so it asks before it acts. Nothing copies your credentials
  anywhere.
- **Container** — Docker is *Auto wolts in a box*. The wolt can run without
  asking because the blast radius is a disposable container and one mounted
  directory.

Everything else — the lodge, the registry, sessions, sites, the TUI, Telegram —
is the same code either way. Choose by how much you want to be asked.

| | Native | Container |
|---|---|---|
| Harness auth | your own, reused in place | seeded into the image |
| Default permission | prompt | auto (opt-in per wolt) |
| Session working dir | any repo on your machine | the mounted wolts directory |
| Survives a platform update | yes — tmux outlives the control plane | container rebuild ends sessions |
| Public URL | tunnel **off** by default | tunnel on by default |
| Data root | `~/.woltspace/wolts` on the host | the same directory, bind-mounted |

---

## Native

```bash
uv tool install 'woltspace[connectors]'
woltspace start          # runs doctor, takes the data-root lock, serves the lodge
woltspace tui            # the terminal UI
woltspace status         # who owns the data root, which sessions were adopted
woltspace stop           # stops the control plane — never touches tmux
```

`woltspace start` runs `doctor` first; every failed check names one command that
fixes it. `stop` deliberately leaves tmux alone: your sessions outlive the
control plane, and the next `start` re-adopts them from the registry.

The `connectors` extra brings the Telegram dependencies. Without it the
Telegram connector reports itself disabled with that remedy instead of
crash-looping.

### Installing before the packages are published

For a step-by-step first run with real expected output, see
[native-first-run.md](native-first-run.md).


`@woltspace/tui` is not on the npm registry yet, so `woltspace tui` cannot fall
back to `npx` — it will fail with what looks like a network error. Until the
first release, install both artifacts from a checkout:

```bash
uv tool install .
cd tui && npm pack && npm install -g ./woltspace-tui-0.2.2.tgz
```

The Python package embeds the exact TUI version it accepts, so a locally
installed binary is used only when its name and version match exactly. When it
does not match, `woltspace tui` says so on stderr and names this recipe before
handing over to `npx`.

---

## Container

```bash
woltspace init      # first-time setup
woltspace start     # start or resume the container
```

The container mounts exactly one directory — your wolts directory — and bakes
everything else into the image. The mount is not optional: a container without
it would come up with an empty data root and every wolt you own silently
missing, so `doctor` and startup both fail with the `docker run -v` command
that fixes it, rather than a traceback.

---

## Channels

Telegram runs as a **channel connector**: a child process the control plane
starts with the API, stops with the API, restarts (bounded) when it dies, and
reports through `woltspace status` and `GET /health`.

The browser terminal's **pty bridge** is a connector too (`tui`). It is the
Node service (`woltspace-tui-service`, shipped inside `@woltspace/tui`) that
`server/app.py` proxies `/tui` websockets to, and it is enabled by default in
both modes — the container no longer starts it by hand. A guest never binds
its port. Switch it off with `channels.tui.enabled = false` or
`WOLTSPACE_TUI_BRIDGE=false`; move it with `channels.tui.port` or
`WOLTSPACE_TUI_PORT`.

> **Three programs are called `woltspace`.** On the host, the bash launcher that
> drives Docker. Inside the container, `container/bin/woltspace` — a thin HTTP
> client, first on `PATH`, with its own `status` that lists sessions and knows
> nothing about connectors. And the packaged native CLI from this Python
> package, which is the one the `woltspace start` / `status` / `tui` commands in
> this document mean. **Inside the container, read connector state with
> `curl -s localhost:7777/health | jq .connectors`** — `woltspace status` there
> is the other program and will answer, confidently, about something else.

Configure it in the data root the control plane owns —
`<wolts>/.space/platform/config.json`:

```json
{
  "channels": {
    "telegram": {
      "enabled": true,
      "token": "123456:your-bot-token",
      "allowed_users": ["11111111"]
    }
  }
}
```

Environment variables override the file (`ENABLE_TELEGRAM_BOT`,
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USERS`), which is how the container
passes its `--env-file` through unchanged. The token is read once, passed to
the connector in its environment, and never written to disk; the status report
carries no secret.

### One token, one poller

**A Telegram bot token can only be long-polled by one process.** A second
poller makes Telegram answer `409 Conflict`, and the loser goes deaf while
still looking alive.

So: while the container colony is running, a native instance must use a
**different** bot token — or stop the container first. If a connector does hit
the clash, `woltspace status` reports it in those terms and stops restarting,
because restarting cannot win that race:

```
connector telegram: degraded · bot.telegram_adapter from …
  error: another process is already polling this bot token (Telegram getUpdates returned 409 Conflict)
  fix: One bot token can only be polled by one process. Stop the other instance …
```

---

## The tunnel

Native runs with the tunnel **off**. Turn it on explicitly:

```bash
WOLTSPACE_PUBLIC_TUNNEL=true woltspace start
```

**Do not reuse the container's named-tunnel token while the container is
running.** Two `cloudflared` connectors on one named tunnel do not conflict —
they *load-balance*. Cloudflare will quietly send a share of your real traffic
to whichever instance answers, so a test instance ends up serving strangers.
Give a second instance its own tunnel, or leave it on `localhost`.

---

## Guests

Only two things are the platform *entrypoint*: `container/start.sh`, and the
control plane that `woltspace start` launches. Both announce it with
`WOLTSPACE_ENTRYPOINT=1`. Anything else that runs `woltspace serve` — a command
typed in a worktree, a smoke test, an agent exploring — is a **guest**, and a
guest is deliberately weak:

- it will not spawn a channel connector on the strength of inherited
  environment variables alone (the data root's own `config.json` must ask for
  it), because the process that exported `TELEGRAM_BOT_TOKEN` is by definition
  already polling that token;
- it never publishes a tunnel, whatever `WOLTSPACE_PUBLIC_TUNNEL` says, because
  the tunnel lifecycle is process-wide and its shutdown would delete the real
  instance's state;
- it refuses outright to serve a data root that shows signs of being in use —
  an owner record with a live pid, a live `tunnel.json`, or sessions marked
  running.

That last refusal is a hard error naming the conflict, not a warning:

```
serve failed: /workspace/wolts is publishing through a live tunnel (cloudflared
pid 4711), so a control plane is already using it. Stop that instance first, use
a fresh data root (`WOLTS_DIR=~/.woltspace/native-wolts woltspace start`), or set
WOLTSPACE_ALLOW_SHARED_DATA_ROOT=1 if you really mean to share it.
```

This matters because the instance lock cannot catch it. A control plane old
enough not to take the lock never held one, and `flock` does not cross a Docker
bind mount — so the evidence a live instance leaves behind is the only thing
that can.

## Sharing a data root

The instance lock is an `flock` on the data root, and **`flock` is not reliable
across a Docker bind mount on macOS**. If your wolts directory is mounted into
a running container, the lock cannot be trusted to catch native-versus-container
contention — two control planes could both believe they own it.

- Your **first native run should use a fresh data root**:
  `WOLTS_DIR=~/.woltspace/native-wolts woltspace start`.
- Pointing native at a **container-mounted data root requires stopping the
  container first**.

`woltspace doctor` warns when the data root already carries another instance's
owner record — including a container's — but treat that as a courtesy, not a
guarantee. The lock is the mechanism; on a shared mount, you are.

---

## Running an existing colony natively — what does not work yet

Field notes from pointing the native control plane at a data root the
container had been running for months (`~/.woltspace/wolts`, 34 wolts). The
lodge, the registry, sessions, sites and the TUI all work. These do not, and
each is a piece of the container entrypoint that native has no equivalent for
yet. Fixing them is the refactor; until then, the workaround column is how to
live with it.

| What | Why | Workaround for now | Intended fix |
|---|---|---|---|
| `push-view`, `notify`, `session-reg`, `wclaude` are "command not found" inside a wolt's session | Sessions inherit the control plane's PATH and nothing adds `container/bin` to it. The container has it on PATH image-wide. | `export PATH=<checkout>/container/bin:$PATH` before `woltspace start` | The session runtime prepends `<install_root>/container/bin` to the session PATH. |
| The Telegram bot starts, then cannot call its model | The control plane reads `channels.telegram` from `config.json` and passes only the token and allowlist to the child. It never reads the data root's `.env`, where `OPENROUTER_API_KEY` / `OPENAI_API_KEY` / `LLM_MODEL` live. The container passes the whole `.env` through `--env-file`. | `set -a; . $WOLTS_DIR/.env; set +a` before `woltspace start` | Either a `channels.telegram.env` / top-level `env` block in `config.json`, or the connector plan reading `$WOLTS_DIR/.env` explicitly. |
| Slack is silent | There is no Slack connector; only Telegram is behind the seam. | Nothing — Slack stays on the container. | A `SlackConnector` beside `TelegramConnector`. |
| No public URL | Native defaults the tunnel off (deliberately). `.env`'s named-tunnel token is ignored unless `WOLTSPACE_PUBLIC_TUNNEL=true`. | `WOLTSPACE_PUBLIC_TUNNEL=true woltspace start` — only with the container stopped, or two connectors load-balance the same hostname. | Document; possibly a `tunnel` block in `config.json`. |
| Wolf crons, the digest, the vulture reaper do not run | `container/entrypoint.sh` starts the creatures; the native supervisor only supervises connectors. | None natively. | Creatures become supervised children like connectors. |
| `woltspace-*` skills are stale or missing | The container entrypoint syncs platform skills into every wolt's `.claude/skills/` on boot. Native never syncs. Existing wolts keep their last container copy; a wolt created natively gets no `.claude/` at all. | Keep booting the container now and then, or copy `container/skills/woltspace-*` by hand. | Skill sync at native start (and in `create_wolt`). |
| Every new session shows Claude Code's workspace trust prompt | Native runs bare `claude`; the container pre-trusts via `wclaude`/`trust-dir`. | Accept it. Prompt mode is the point of native. | Probably nothing; document it. |
| `status` says `N orphaned` on first start | The registry still marks sessions the container was running. Their tmux sessions never existed on the host, so adoption orphans them. | Nothing; it is correct. | — |
| Wolt `CLAUDE.md` / memory files mention `/workspace/wolts/...` | Written from inside the container. Registry records are normalized on adoption; prose is not. | Cosmetic. | — |
| Old wolts' skills refer to `/workspace/woltspace` | Same. | Cosmetic until a skill shells out to that path. | Skills should use `$WOLTSPACE_DIR`. |
| `WOLTSPACE_PUBLIC_TUNNEL` reads as "expose me to the internet" | It is the historical on/off switch for ANY tunnel. With `CLOUDFLARE_TUNNEL_TOKEN`+`URL` set it runs the NAMED tunnel — login-gated by Cloudflare Access at the edge, not public at all. The name genuinely alarmed the first native operator. | Know that token+URL present ⇒ named/Access-gated; the random public trycloudflare URL happens only with NO token. | Rename (e.g. `WOLTSPACE_TUNNEL=off\|named\|quick`) with the old var honored as an alias. |
| A plain `uv tool install -e .` silently drops the telegram connector | The bot needs the `connectors` extra; without it the connector reports "python-telegram-bot is not installed" and chat goes dark. | Always install `-e '.[connectors]'` from a checkout. | `status`/`doctor` already print the exact remedy — maybe `start` should refuse loudly when config enables a channel the install cannot run. |
| Entering a session from the tui inside your own tmux moves your whole client there | The tui was designed to own the terminal. Same-server entry now uses `switch-client` (no more nested clients), but a switch teleports you out of the windows you were working in. | The detach key (`ctrl-\`) switches back to where the tui lives. | A "peek" mode: `link-window` grafts the wolt session in as a window of the *current* session — flip to it and back, workspace never moves. Real edges (window numbering, one window in two sessions, unlink discipline), so it is a designed feature, not a patch. |

Pivoting between the two is safe as long as only one runs at a time (same
data root, same port, and the instance lock cannot see across the Docker
mount): `woltspace stop` natively **before** `woltspace start` for the
container, and stop the container before the native `start`. The native
`stop` clears its owner record and takes its connectors down with it; a
container that finds a live-looking native owner record refuses to serve.

---

## Release checklist

Publishing is a single coordinated moment, and it happens only on an explicit
human go-ahead.

1. **Run the opt-in integration probes.** They cover the seams nothing else
   does — a real agent spawn, a real Telegram round-trip, real session revival
   — and they are skipped by default because they boot agent processes and
   message a real chat, not because they are optional:

   ```bash
   TEST_CHAT_ID=<your test group> bash test/run-tests.sh opt-in
   ```

   Every spawn goes into a throwaway `test-shadow` wolt that the fixture
   creates and removes, so no real wolt is touched and no agent survives the
   run. Every message goes to the chat you named: the suite never discovers a
   chat id from live state, so an unset `TEST_CHAT_ID` skips rather than
   guesses.
2. **Bump the version in all three places** — they are pinned exactly, and the
   cross-manifest tests fail on drift:
   - `tui/package.json`
   - `tui/src/version.js`
   - `src/woltspace/compatibility.py`
3. Build both artifacts and clean-install them **outside the checkout**: wheel
   and sdist into fresh venvs, `npm pack` tarball into an isolated npm prefix.
   Check both bins answer: `woltspace-tui --version --json` and
   `woltspace-tui-service --version --json`.
4. **Publish PyPI and npm together.** The Python package embeds the exact npm
   version, so a half-published release is a broken `woltspace tui`.
5. **Build the OCI image from the released artifacts** — not from a checkout —
   so the container ships the same bytes as the native install.
6. Remove the pre-publish install recipe above once `npx` can resolve
   `@woltspace/tui`.
