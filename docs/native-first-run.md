# First run on macOS, from a checkout

For jerpint and whoever is pairing with him. This is the walkthrough for
running Woltspace natively on a Mac — no Docker — before either package is
published.

**Stop the container first.** Two things below depend on it: the Telegram bot
token can only be polled by one process, and the instance lock is not reliable
across a Docker bind mount. Details in
[native-and-container.md](native-and-container.md); the short version is that
this walkthrough uses a **fresh data root** and never touches the container's.

> **What was and was not verified.** Every command and every quoted output
> below was actually run — inside the Linux dev container, from this branch,
> with an isolated tool dir, npm prefix, data root, and port. What that cannot
> cover is **macOS itself**: Homebrew installs, Apple Silicon wheels, the
> macOS `ps`/tmux builds, and Gatekeeper. Steps marked **(untested on macOS)**
> are the ones where the Mac could still differ.

---

## 1. Prerequisites

You need three things on PATH: `tmux`, Node 18+, and `uv`.

```bash
tmux -V
node --version
uv --version
```

Anything missing **(untested on macOS)**:

```bash
brew install tmux node
curl -LsSf https://astral.sh/uv/install.sh | sh
```

You also need a harness you are already logged into — `claude`, `codex`, or
`opencode`. Native mode reuses that login in place; it never copies your
credentials anywhere. If you are not logged in yet, log in the normal way for
that CLI first.

---

## 2. Install both artifacts from the checkout

`@woltspace/tui` is not on the npm registry yet, so `woltspace tui` cannot fall
back to `npx` — install the TUI from the checkout too.

```bash
cd /path/to/woltspace
uv tool install .
cd tui && npm pack && npm install -g ./woltspace-tui-0.2.2.tgz
```

`npm pack` prints the file list and leaves `woltspace-tui-0.2.2.tgz` in `tui/`;
delete it afterwards if you do not want it lying in the checkout.

Check both halves agree:

```console
$ woltspace --version
woltspace 0.2.2

$ woltspace-tui --version --json
{"name":"@woltspace/tui","version":"0.2.2","binary":"woltspace-tui"}
```

The Python side embeds that exact name and version and accepts a local binary
only on an exact match, so if these two disagree, fix it here rather than
later.

> If `uv tool install` warns that its bin directory is not on your PATH, follow
> the `export PATH=...` line it prints (or run `uv tool update-shell`) and open
> a new shell. **(untested on macOS)**

---

## 3. Point it at a fresh data root

Do **not** point the first native run at `~/.woltspace/wolts` while the
container has it mounted. Use a new directory:

```bash
export WOLTS_DIR=~/.woltspace/native-wolts
```

Confirm what it resolved:

```console
$ woltspace paths
wolts_dir: /Users/you/.woltspace/native-wolts
state_root: /Users/you/.woltspace/native-wolts/.space
install_root: /Users/you/.local/share/uv/tools/woltspace/lib/python3.11/site-packages/woltspace/_bundle
endpoint: http://127.0.0.1:7777
isolation: host
```

`install_root` pointing inside the installed package is correct — that is the
bundled server and web assets, not your checkout.

---

## 4. Preflight

```console
$ woltspace doctor
✓ python: Python 3.11.2
✓ package: runtime and web assets present
✓ data-root: /Users/you/.woltspace/native-wolts (nearest existing parent: /Users/you/.woltspace)
✓ tmux: /usr/bin/tmux
✓ harness: claude=/Users/you/.local/bin/claude, codex=/usr/local/bin/codex
✓ host-auth: claude, codex
✓ port: 127.0.0.1:7777 is available
```

Doctor only reads. Every failure names one command that fixes it; work down
the list until it is clean.

Two checks worth understanding:

- **host-auth** is a warning, not a failure. It means Woltspace found an
  existing harness login it can reuse. If it says nothing was detected, log in
  with that CLI — Woltspace will never copy a credential file for you.
- **data-root-sharing** only appears if the directory is already claimed by
  another instance, including a running container. If you see it, stop that
  instance or pick a different `WOLTS_DIR`.

---

## 5. Start

```console
$ woltspace start
woltspace started: http://127.0.0.1:7777
wolts: /Users/you/.woltspace/native-wolts
logs: /Users/you/.woltspace/native-wolts/.space/logs/control-plane.log
status: woltspace status
```

`start` runs doctor first and refuses to launch if it fails. Add `--port 7788`
if 7777 is taken.

```console
$ woltspace status
state: healthy
endpoint: http://127.0.0.1:7777
wolts: /Users/you/.woltspace/native-wolts
owner: pid 67618 · bd6929db67a34a8bb0ab484b632ceff3 · your-macbook
adoption: 0 live · 0 orphaned · 0 unchanged
connector telegram: disabled · disabled
  fix: Set channels.telegram = {"enabled": true, "token": "<bot token>"} in /Users/you/.woltspace/native-wolts/.space/platform/config.json (or export TELEGRAM_BOT_TOKEN).
```

`adoption: 0 live` is right on a first run — there are no sessions to adopt
yet. On later restarts this is how you see that live sessions were picked back
up.

Open <http://127.0.0.1:7777> and the lodge should be there.

---

## 6. The TUI

```console
$ woltspace tui --dry-run
source: local
package: @woltspace/tui@0.2.2
command: /Users/you/.npm-global/bin/woltspace-tui
```

`source: local` means it found your installed TUI and its identity matched
exactly. Then run it for real:

```bash
woltspace tui
```

From there: create or select a wolt, confirm the working directory and
permission policy it shows you, spawn, and attach. Detach with tmux's usual
`Ctrl-b d` and you are back at the list; the session keeps running.

If `--dry-run` says `source: npx` instead, it did not accept your local
binary and will try to download from the registry — which cannot work yet. It
tells you so:

```console
$ woltspace tui --dry-run
source: npx
package: @woltspace/tui@0.2.2
command: /usr/local/bin/npx --yes --package=@woltspace/tui@0.2.2 woltspace-tui
woltspace: ignoring /nonexistent/woltspace-tui — [Errno 2] No such file or directory: '/nonexistent/woltspace-tui'; resolving @woltspace/tui@0.2.2 through npx instead.
woltspace: if that fails because @woltspace/tui@0.2.2 is not published yet, install both artifacts from a checkout: uv tool install . && cd tui && npm pack && npm install -g ./woltspace-tui-0.2.2.tgz
```

Redo step 2 if you see that.

---

## 7. Telegram, if you want it

**Use a different bot token than the container's.** One token can only be
long-polled by one process; the second poller gets `409 Conflict` from
Telegram and goes deaf while still looking alive. Make a second bot with
BotFather for native testing.

The Telegram dependencies come from an extra:

```bash
uv tool install 'woltspace[connectors]'
```

Then write `$WOLTS_DIR/.space/platform/config.json`:

```json
{
  "channels": {
    "telegram": {
      "enabled": true,
      "token": "123456:your-test-bot-token",
      "allowed_users": ["your-telegram-user-id"]
    }
  }
}
```

Restart and check:

```bash
woltspace stop && woltspace start
woltspace status
```

`connector telegram: running · bot.telegram_adapter from … · pid N` means it
is up. If it says `degraded` with a 409, the other bot is still polling that
token. If it says `disabled · enabled but python-telegram-bot is not
installed`, you skipped the extra — this is the exact output when that happens:

```console
connector telegram: disabled · enabled but python-telegram-bot is not installed
  fix: Reinstall with the connectors extra: `uv tool install 'woltspace[connectors]'` (or `pip install 'woltspace[connectors]'`), then `woltspace start`.
```

**Leave the tunnel off.** Native defaults to no tunnel, and you should not
reuse the container's named-tunnel token while the container runs: two
cloudflared connectors on one named tunnel *load-balance*, so real traffic
would start landing on the test instance.

---

## 8. Stopping

```console
$ woltspace stop
control plane stopped; tmux sessions untouched
```

That last clause is deliberate: stopping the control plane does not kill your
wolts. Their tmux sessions keep running, and the next `woltspace start`
re-adopts them from the registry — `woltspace status` will show them under
`adoption`. To end a session, stop the session, not the control plane.

---

## Troubleshooting

Start with `woltspace doctor` and `woltspace status`; between them they cover
most of it.

| Symptom | What it means |
|---|---|
| `start failed: doctor-failed` with a list | A prerequisite is missing; each line names its fix. |
| `port: 127.0.0.1:7777 is already in use` | Something else has the port — `woltspace start --port 7788`. |
| `woltspace already running: http://127.0.0.1:PORT` | An instance owns this data root. That URL is where it actually serves, which may not be the port you asked for. |
| `state: stale` | A previous control plane died without cleaning up. `woltspace stop` clears the metadata; it signals nothing and leaves tmux alone. |
| `data-root-sharing` warning | Another instance — likely the container — claims this directory. Stop it or use a different `WOLTS_DIR`. |
| `serve failed: … is not mounted` | Container mode without the wolts mount. Not applicable to a native run. |
| Connector `degraded` with a 409 | Another process is polling that bot token. |
| Connector `failed` after several restarts | It could not stay up; read `.space/logs/connector-telegram.log`. |

The control plane's own log is `$WOLTS_DIR/.space/logs/control-plane.log`.

### Confirming it wrote no credentials

The point of native mode is that your harness login is reused, never copied.
After a first run, the data root should contain state and logs and nothing
resembling a credential:

```console
$ find "$WOLTS_DIR" \( -name "*credential*" -o -name "auth.json" \) | wc -l
0

$ find "$WOLTS_DIR"
<wolts>
<wolts>/.space
<wolts>/.space/logs
<wolts>/.space/logs/control-plane.log
<wolts>/.space/platform
<wolts>/.space/platform/adoption.json
<wolts>/.space/platform/connectors.json
<wolts>/.space/platform/control-plane.lock
```

If a credential file ever shows up there, that is a bug worth stopping for.
