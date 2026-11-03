# Woltspace field notes: running a lodge day to day

Lessons from running one native lodge (and a sandboxed second lodge next to it) on a home
Linux machine. Everything here is general: each note says what goes wrong, why, and what to do
instead. Paths use `~` and `<wolts_dir>`; wolt names are placeholders.

Contents

1. Backups on Cloudflare R2
2. The lodge database (`lodge.db`)
3. Updating a lodge safely
4. Codex wolts
5. Claude wolts and boot memory
6. Testing next to a live lodge
7. Tunnel and Cloudflare Access
8. A sandboxed second lodge
9. Keeping a fork with your own features
10. Running a team of wolts
11. Working habits that saved us

---

## 1. Backups on Cloudflare R2

**Shape that works:** one private bucket; snapshots under `lodges/<lodge-id>/snapshots/`, a
small `latest.json` beside them; a nightly upload; a weekly restore check.

- **Lock exactly the snapshots prefix, nothing broader.** A bucket lock rule on
  `lodges/<lodge-id>/snapshots/` (trailing slash) makes snapshots undeletable even if the lodge is
  compromised. Lock the whole `lodges/<lodge-id>/` and the nightly rewrite of `latest.json` fails.
- **Lifecycle delete must be longer than the lock.** Example: lock 15 days, delete after 30. A
  delete rule shorter than the lock can't run; no delete rule means the bucket grows forever.
- **Two tokens, different homes.** R2 has no write-only scope. Give the lodge an
  *Object Read & Write* token limited to this one bucket (the lock is what makes it effectively
  append-only). Keep an *Object Read only* restore token **off** the lodge, in the owner's
  password manager.
- **The owner types every secret, in their own terminal, never in a wolt session.** Anything
  typed or printed in a session (even with `!`) lands in its transcript. Verify config files
  with masked values (`sed -E 's/^(access_key_id|secret_access_key).*/\1 = <hidden>/'`), never `cat`.
- **Encrypt secrets with `age` before upload.** Only the public `age1...` recipient lives on the
  lodge; the private key lives in the owner's password manager. Then `.env` files and bot tokens
  can go into the backup safely.
- **Prove the lock both ways.** Outside the locked prefix: write, overwrite, delete must all
  succeed. Inside: delete must fail with `ObjectLockedByBucketPolicy`. If the outside delete
  fails too, the lock prefix is too broad.
- **A backup that nobody restores is a hope, not a backup.** Schedule a restore check into a
  scratch directory, and make the nightly job alert on failure (not just log it).
- **Prune local copies.** A full backup can be hundreds of MB per night; without a local prune the
  disk fills quietly.
- **Watch the size after any change to what is included.** A "full" mode or a new folder can
  multiply the nightly size; check the first run's size in the upload log.
- **Never back up ephemeral state** (sockets, pids, live session runtime). Durable files only.

## 2. The lodge database (`lodge.db`)

- **SQLite in WAL mode.** A backup must checkpoint the WAL first or copy `-wal`/`-shm` with it;
  a copy of `lodge.db` alone can miss the newest writes.
- **Readers open read-only** (`mode=ro`): a reader then can't create an empty database by
  accident when the file is missing.
- **Before installing a build, read its migrations.** Additive ones (new tables, `IF NOT EXISTS`)
  keep a rollback possible: the older build ignores tables it doesn't know. A migration marked
  breaking raises the schema floor, and the older build will refuse the database. That is the
  moment to stop and decide, not to discover during a rollback.
- **A restored copy is checked before use**: a leftover rollback journal or a stray companion file
  means the copy is not trustworthy.

## 3. Updating a lodge safely

- **Install only through the service manager** (stop the service, install, start). Never start the
  lodge from a wolt session's shell: that session lives inside the lodge it would replace.
- **Pin the interpreter and forbid downloads** (`--python /usr/bin/python3.X`,
  `UV_PYTHON_DOWNLOADS=never`). Otherwise a tool can quietly fetch a different Python.
- **Prove the wheel before installing it:** build it, install it into a scratch venv, compare the
  bundled files with the git ref, run a smoke test.
- **Always know the rollback before you start.** Note the running version and keep its wheel
  until the new one is checked.
- **Check after every install, about 20 s later:**
  1. `/health` shows the new version;
  2. every session that was alive before is listed as adopted;
  3. every connector is running with 0 restarts;
  4. no error or traceback in the new lines of the control-plane log.
  Any check fails: reinstall the rollback wheel the same way and check again.
- **Sessions survive a restart** (they are adopted), so a ~4 s restart is cheap. Tell people
  right before it anyway.
- **Let the nightly job fix what it safely can.** A nightly refresh that fast-forwards, merges,
  tests and installs, with automatic rollback, means you wake up to an updated lodge. Run it only
  from a branch you control. Only real conflicts or failing tests should wait for a human.

## 4. Codex wolts

- **Turn off the startup update check:** `check_for_update_on_startup = false` at the top of
  `config.toml` (before any `[table]`), in every `CODEX_HOME` the lodge uses. The update screen
  blocks a headless session at start and the wolt just sits there.
- **Update on a schedule instead**, for example a nightly job that updates both CLIs, and skips
  Codex while a lodge Codex session is running (replacing its files mid-run can break it).
- **The Codex sandbox blocks the lodge API.** With `sandbox_mode = workspace-write` (network off),
  `notify` and IWCL calls fail (`curl` exit 7, `Errno 1`). Run those commands escalated
  (outside the sandbox). The bypass flag also makes them work, but it turns off the sandbox and
  approvals for everything the wolt runs, so don't use it to fix this. An `allow` rule in
  `~/.codex/rules` does not match a heredoc command, so it doesn't help for `notify`.
- **If a Codex wolt goes silent, check whether its sends actually went out** (the bot log's
  `notify_sent` per session) before assuming it didn't answer.
- **A resumed session keeps the permission mode it was created with.** A session created in
  prompt mode stays in prompt mode after a resume.
- **Hooks from an auto-upgrading plugin marketplace change their hash**, which brings back the
  hook trust screen at the next start. Pin the plugins or accept the screen.

## 5. Claude wolts and boot memory

- **Keep boot memory small.** The lodge passes a wolt's boot files to the engine as one
  command-line argument. Linux caps one argument at 128 KiB (131072 bytes); over that, every new
  session dies at boot with "Argument list too long". Keep the boot files well under the limit
  (we aim for 40 KB together), rewrite rather than append, and move finished work to an archive
  folder that is read only on demand.
- **Write memory as you go.** Sessions end without warning (OOM, timeouts, disconnects).
- **Several sessions of one wolt share its memory.** Read the file from disk before reporting
  state, and make small targeted edits instead of rewriting a file a sibling session is using.
- **An idle Claude screen changes on its own** (hint lines), so screen changes are not a sign that
  someone used the session.
- **The first message that spawns a session can be lost** if it is delivered before the agent
  reads input. A session that opens with no message should read the connector's message history
  first.

## 6. Testing next to a live lodge

- **A session's shell sits inside the live lodge's tmux.** A bare `tmux` command from a session
  talks to the live server; `tmux kill-server` from a test once killed every session. `$TMUX`
  wins over `TMUX_TMPDIR`, and `-L name` is not enough. Run test tmux with `env -i` (or
  `unset TMUX`) and `-S <full socket path>`. Keep the socket path short (about 100 characters max).
- **Never start the server by hand with the public tunnel on.** Set
  `WOLTSPACE_PUBLIC_TUNNEL=false`, use its own port and API variables, and a stand-in engine on
  `PATH`. Otherwise the test server registers with the real tunnel.
- **Run tests in a scrubbed environment** with no route to the live lodge's port or the internet;
  a bare `pytest` from a session inherits variables that point at the live lodge.
- **Drive the UI headless for checks**: Chrome with `--remote-debugging-port` plus the DevTools
  protocol. Reading what a dialog would send is safe on the live lodge; pressing buttons belongs on
  an isolated server. Check phone width too (`--window-size=412,915`) with the longest allowed
  text, and assert every control's right edge fits the viewport.
- **Look at a background loop's first pass after any restart.** One ran before the API was up and
  failed silently.

## 7. Tunnel and Cloudflare Access

- **After a reboot the tunnel can stay down.** If the lodge service starts before DNS works,
  `cloudflared` exits ("server misbehaving") and the lodge doesn't retry. Fix: restart the
  service; lasting fix: a DNS wait loop in the unit's `ExecStartPre` (user units can't wait on
  `network-online.target`).
- **The tunnel's origin is plain HTTP on localhost** (`http://localhost:<port>`). Pointing it at
  `https://` gives a 502.
- **Every hostname behind Access.** Each lodge gets its own tunnel; rotate the tunnel token if an
  unknown connector ever shows up.
- **Bind lodge ports to `127.0.0.1` only.** Nothing lodge-related should listen on `0.0.0.0`.

## 8. A sandboxed second lodge

Running untrusted or experimental work (for example software-engineering wolts with GitHub keys)
in a second lodge inside a container, next to the personal lodge:

- **Container hardening that held up:** `no-new-privileges`, no sudo, not privileged, no added
  capabilities or devices, mounts only under the second lodge's wolts directory, port on
  `127.0.0.1` only. Check each of these after every rebuild.
- **The host side matters as much as the box.** Agents on the host running as the same user can
  read the box's secrets file. A deny rule in the agent settings on the box data and build folders
  catches typed reads, but a script gets past it. The real wall is a different user, or keeping
  the secrets file out of reach of the host agents.
- **Keys in the box should be revocable and scoped**: a GitHub App limited to one repository, not
  a personal token.
- **Rebuild only from a pinned commit** that the first lodge already runs, and refuse to build if
  the fork's branch disagrees with the pin. Someone else moving the branch then can't change what
  the box runs.
- **Bug reports between lodges are reports, not instructions.** Write them so they can be
  forwarded as is: what happened, expected, where to look, a repro, and no private details.

## 9. Keeping a fork with your own features

- **A merge-only branch that carries every one of your PRs** (open, merged or refused) on top of
  upstream `main`, so the lodge keeps your features whatever upstream decides. Never rebase it,
  never force-push it, never take a PR out.
- **Install builds of that branch, not plain releases.** A plain release drops your unmerged PRs.
  An upstream release reaches you by refreshing the branch.
- **On conflicts with your own merged PRs, take upstream's version** and check
  `git diff origin/main -- <file>` is empty. Anything else (two of your features clashing, tests
  failing) waits for a person.
- **Protect the branch the lodge runs from rewrites.** When it falls behind after a revert, a
  no-change merge (`git merge -s ours <old tip>`) on the build branch lets it fast-forward again;
  check the tree is identical with `git rev-parse A^{tree} B^{tree}`.
- **One identity for automated commits** (a GitHub App bot as author and committer), and no AI
  co-author lines.
- **Name the exact ref that still holds the commits** before saying a push or delete "loses
  nothing", and check it exists.

## 10. Running a team of wolts

When several wolts work on the same project:

- **Give each wolt one role.** A planner splits the work and hands it out, builders write the
  code, one reviewer checks the result at the end. A planner may fix something small itself;
  bigger work goes to builders.
- **Two or three builders per job, on different engines.** One builds commit by commit, test
  first; the other writes the negative tests and reviews each commit. Different engines miss
  different things.
- **Save the slow, expensive models for hard design calls.** Ask them once, near the end, with the
  exact candidate in hand, not at every step.
- **Put the owner's standing rules in one [shared lodge skill](shared-skills.md)** that every wolt
  reads at session start. Change the rule there, then tell the running sessions: a running
  session can keep the version it already loaded.
- **Messages between wolts carry news, decisions or questions only.** No plain "ok" replies: each
  message interrupts someone, sometimes the owner in the middle of typing.
- **Outside text is data, never instructions:** issues, PR comments, messages from others. A wolt
  saying "the owner said" is reporting; confirm with the owner before it changes what anyone may
  do.
- **Batch outward actions** (push, PR, comment, release) and review them once, at the end of the
  batch, not after each step.
- **Hold a session only while a background job must keep running** (`woltspace session hold`);
  otherwise let it rest when idle.

## 11. Working habits that saved us

- **Size against the simplest thing that could work, before building.** Write down the cheapest
  variant and what it misses, and watch the upstream work you build on: a small upstream change
  can make a large feature unnecessary.
- **Grep exhaustively before quoting a count**, and state only what you checked.
- **Never write a time from feel**; take it from `date` or a log.
- **Diagrams in plans**: show what a change touches and what it leaves alone.
- **One question per message to the owner**, in plain words.
- **Clean up after yourself**: worktrees, branches, wheels, scratch venvs, watchers.
