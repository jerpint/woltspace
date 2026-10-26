# Backup and restore

A woltspace backup is a snapshot of the **data plane**: one `tar.gz` holding the
wolts directory and a manifest describing it. No container, no image, no caches.

```bash
woltspace backup                       # → woltspace-backup-<UTC timestamp>.tar.gz
woltspace backup --tag pre-rc5         # name it yourself
woltspace backup --out ~/snapshots     # somewhere other than beside the wolts dir
woltspace restore ~/snapshots/woltspace-backup-pre-rc5.tar.gz
```

Both commands work the same natively and inside the container — they only ever
touch files. Take one before an upgrade.

With `WOLTSPACE_BACKUP_AGE_RECIPIENT` set, the backup is one
`woltspace-backup-<tag>.tar.gz.age` instead: the same tar, secrets included,
encrypted to that key, and restored with `--identity` (see "Encrypted
backups").

## What goes in

Everything under `WOLTSPACE_WOLTS_DIR` that is data:

- each wolt's `wolt/` tree — `memory/`, `site/`, `sparks/`, `drafts/`, `apps/`
- `CLAUDE.md` and `wolt.json` per wolt
- `woltspace.json` (but not `.env`, unless the backup is encrypted — see
  "Secrets are not backed up")
- `.state/` (session registry, wolf state) and `.space/` (platform metadata)
- `.space/lodge.db`, the lodge's database, as a snapshot taken with SQLite's
  backup API into memory (never a temporary file): consistent even while the
  lodge writes to it. Its live `lodge.db-wal`
  and `lodge.db-shm` stay out. Any other SQLite file (an app's own) is archived
  as it lies, `-wal` included, because that is where its newest rows are.
  Every row goes in, chat routing, the Slack queue and the Auto and dig
  consents included. On restore, the apps' pids (and tunnel pids) are blanked
  in the restored copy: on another machine an old pid can be any live
  process, so the lodge starts the app again instead of trusting it
  (pre-lodge.db app files, `.space/apps/<name>.json`, come back as they were)
- logs, chat history, spools and the `.imported` copies lodge.db's importers
  leave behind: everything except junk (below) and the two OAuth login files
- `.git` directories — a wolt's history is data, so it is archived in full

Symlinks are archived **as symlinks** and never followed. The skill-delivery
links point into the installed wheel; following them would swallow the platform
a backup is not supposed to be backing up.

## What is excluded, and why

Two kinds of rule, and the manifest reports them separately because they are
not interchangeable.

**By directory name** — dropped wherever the name appears, but only *below* the
top level (see "The root is different"):

| Excluded | Why |
| --- | --- |
| `node_modules`, `.venv`, `venv`, `.npm` | reinstallable from a lockfile |
| `__pycache__`, `.cache`, `.pytest_cache` | derived, per-machine |
| `dist`, `build`, `.next`, `target` | build products |
| `.worktui` | worktui's worktree store; the branches live in git |

**By path** — matched on the whole tail, never on a bare name:

| Excluded | Why |
| --- | --- |
| `.claude/plugins/cache` | plugin download cache |
| `wolt/worktrees` | a wolt's throwaway checkouts |
| `.local/share/claude` | the agent CLI's auto-downloaded version binaries |
| `.codex/shell_snapshots` | codex's runnable env snapshots — rebuilt every session, and every exported secret is baked into them |

`.local/share/claude` is the single biggest thing in a lived-in colony —
**12.8GB** of redownloadable CLI binaries across per-wolt HOMEs on the colony
these rules were measured against. It is scoped by path deliberately: a wolt's
own directory called `claude` is data, the rest of `.local` is data, and
`.local/share/opencode` (session state, easily 150MB) is kept. Conversation
transcripts live in `.claude/projects`, which is also kept.

A restored lodge comes back with its consents. An Auto grant names a wolt
and an absolute working folder, so it applies again only where that path
exists the same way; a dig re-checks its host, user and port with `ssh -G`
before every connection. `restore` says to review them (`woltspace auto
list`, `woltspace dig list`) and revoke what the new machine should not have.

**By file** — `.DS_Store`, `*.pyc`, `*.sock`, and anything that is not a
regular file, a directory, or a symlink (sockets, fifos, devices): noise, or
meaningless once restored.

## Secrets are not backed up

Credentials are **withheld**. Not excluded as junk — withheld, listed, and
handed back to you as a checklist.

| Withheld | Rule |
| --- | --- |
| `.env`, `.env.local`, `.env.production`, … | `.env` or `.env.*` at any depth (apps carry their own), except `.example` and `.sample` templates |
| `.claude/.credentials.json`, `…json.expired-*.bak`, `…json.stale-*.bak` | `.credentials.json*` directly inside a `.claude` directory |
| `.codex/auth.json` | `auth.json` directly inside a `.codex` directory |
| `.space/platform/config.json` | the lodge's own (bot tokens), at the top of the data root only |

**Why, and it is not just tidiness.** A rotating OAuth chain has exactly one
live owner. Restoring a *stale* credentials file replays a refresh token that
has already been spent, reuse-detection sees a replay, and the chain gets
revoked — including the live session you were still using. A backup that
restores credentials can take down the colony it was meant to protect. Second,
an archive sitting on a disk or in a bucket should not be a bearer token at
rest.

The rules are explicit names and paths. Nothing sniffs file contents: a
heuristic that reads files looking for secrets is a heuristic that reads
secrets, and one that guesses wrong drops data. Templates and lookalikes —
`.env.example`, `.env.sample`, `env.example`, `credentials.md` — are ordinary
files and are backed up.

There is no `--with-secrets` flag. The `.env` files and `config.json` can
travel **encrypted**, inside an encrypted archive, never in the clear: see the
next section. The OAuth credential files never travel at all.

### Encrypted backups

Set `WOLTSPACE_BACKUP_AGE_RECIPIENT` to an [age](https://age-encryption.org)
public key and have `age` on `PATH`, and the backup becomes **one file**,
`woltspace-backup-<tag>.tar.gz.age`: the whole tar, with the `.env` files and
`.space/platform/config.json` inside it, streamed through `age -r` into a file
created owner-only before age writes a byte. No plaintext copy of anything is
written to disk, not even a temporary one. The manifest is inside the
ciphertext too; nothing about the lodge sits beside it in the clear.

```bash
age-keygen -o ~/lodge-backup-key.txt     # on a machine of yours, then keep it OFF the lodge
export WOLTSPACE_BACKUP_AGE_RECIPIENT=age1...   # the public key it printed
```

The lodge only ever holds the public key, so it can write backups and never
read them. Lose the private key and every encrypted backup is lost with it:
keep it in a password manager. A recipient set without `age` on `PATH` fails
the backup rather than fall back to a plaintext archive of the whole lodge; a
recipient `age` rejects fails it too. Without a recipient the backup is the
plain `.tar.gz` described above, with the secrets withheld and the manifest
saying why.

A `.env` that is a symlink is never read through: it is listed as skipped and
stays on the restore checklist.

**What this does not do:** session transcripts (`.claude/projects`,
`.codex/sessions`) are kept, and a transcript can contain a secret somebody
echoed into a session. That exposure exists on disk whether or not you ever take
a backup, and the history is real data — the policy is credential *files*
withheld, history kept. If a secret was echoed into a session, rotate it; do not
count on the backup to have forgotten it.

**A restore never leaves you guessing.** The manifest's `withheld` section
lists every path that was left behind (paths only, never contents), the backup
summary prints the count, and `woltspace restore` ends with the re-provision
checklist:

```
withheld from this backup — 3 credential file(s), by design:
  claude-credentials: re-authenticate Claude: `woltspace init`, or `claude login`
    .claude/.credentials.json
  codex-auth: re-authenticate codex: `codex login`
    beaverwolt/home/.codex/auth.json
  dotenv: recreate it — copy .env.example and fill in the tokens
    .env
  (a stale rotating token, restored, can revoke the live one)
```

## The root is different, and a wolt is never junk

A directory called `dist` is junk inside a project and a perfectly good name
for a wolt. Two structural rules keep a name from ever costing you data:

1. **At the top level of `WOLTSPACE_WOLTS_DIR`, nothing is excluded by name** except what
   the platform derives there — `.worktui`. Everything else the platform keeps
   at the root (`.space`, `.state`, `.claude`, `.codex`, `apps`, `projects`,
   `woltspace.json`) is data and stays — minus the individual credential files
   inside them, which are withheld by the rules above.
2. **A wolt is never excluded**, wherever it sits — nor is a subtree that has a
   wolt somewhere underneath it. A directory counts as a wolt on the same
   signal discovery uses: a `wolt/wolt.json` (or a `wolt/` directory, or a
   `wolt.json`).

Backing up too much costs disk. Backing up too little loses a wolt.

The summary prints the scanned size and the excluded size side by side, so the
win is visible on every run:

```
data: 4.7MB in 612 files (scanned 391.2MB, excluded 386.5MB in 8104 files)
archive: 1.2MB · 688 entries
  wolt beaverwolt: 3.9MB in 480 files (−370.1MB excluded)
verified: manifest parses, entry count matches, wolt.json readable
```

## The manifest

`backup-manifest.json` sits at the root of the archive, beside the `wolts/`
tree, and records: creation time, host, woltspace version, tag, source path,
the wolt list with per-wolt sizes, total entry count, the exclude rules that
were applied, and any paths that were skipped.

It also carries a `withheld` section naming every credential path the backup
left behind. Paths only: no credential is in the archive, and nothing reads a
secret's **value** into the manifest or the printed summary.

Two more sections: `lodge_db` (when there is a `lodge.db`) counts the rows of
each table in the snapshot, `{"tables": {"sessions": 159, ...}}`; `secrets`
says whether the `.env` files and bot tokens are inside, `{"present": true,
"recipient"}` for an encrypted backup, and if not, the `reason`.

## Trust but verify

A backup nobody has opened is a rumour. Every run re-opens the archive it just
wrote, parses the manifest back out of it, checks the member count against what
the manifest claims, and spot-reads one `wolt.json`. A mismatch is an error, not
a warning. An encrypted backup cannot be re-opened on the lodge, which has no
key: the run checks that `age` exited cleanly and the file starts with the age
header, and a restore with the key is the real check (see "The restore
checks").

Unreadable files are the opposite: they warn, they are listed in the summary and
recorded in the manifest, and the backup still completes.

## Restore

```bash
woltspace restore woltspace-backup-pre-rc5.tar.gz [--to DIR]
woltspace restore woltspace-backup-<tag>.tar.gz.age --identity ~/lodge-backup-key.txt [--to DIR]
pass show lodge-age | woltspace restore woltspace-backup-<tag>.tar.gz.age --identity -
```

Restore extracts into a **new** directory — by default
`<archive-stem>-restored` beside the archive — and refuses to write into an
existing non-empty one. There is no `--force`. The archive's tree lands at
`<target>/wolts/`, and the command prints the exact line to boot from it:

```
boot from it with:
  WOLTSPACE_WOLTS_DIR=/path/to/woltspace-backup-pre-rc5-restored/wolts woltspace start
```

Pointing a colony at a restored directory is the human's explicit act, never a
side effect of unpacking a file.

`lodge.db` comes back in place with the rest of `.space/`, and from an
encrypted backup so do the `.env` files and `config.json`. The key is read
once, from the file or stdin, and handed to `age -d` on its stdin; it is never
written anywhere. The archive is decrypted twice, never to disk: once to audit
every member (below), then again to extract, and the second pass must see
exactly the members the first one audited.

On the first boot the lodge finds no tmux behind the sessions that were
`running` and marks them `orphaned`; `resting` sessions keep their conversation
id. Re-authenticate the harnesses, as the checklist says.

### What a restore refuses

Symlinks are archived as links, including ones pointing outside the tree, so
the extractor cannot rely on the stdlib `data` filter (which rejects exactly
those). Instead the whole member list is audited before a single byte lands,
and the archive is refused outright if it contains:

- an **absolute path** or a `..` component;
- a **hard link** — no archive written here contains one, so any that shows up
  came from elsewhere and its target would be resolved by the extractor rather
  than by us. Colony data *does* contain hard links (Claude's `file-history`
  dedupes identical file versions across sessions that way); the backup stores
  every regular file whole instead of linking the second name to the first, so
  a restore yields two independent files with equal content. The duplication is
  bounded — the big hard-link populations live in excluded directories;
  `file-history` is tens of megabytes.
- a member whose path **runs through a symlink** the same archive creates
  (`x` → elsewhere, then `x/evil`), or that **replaces** one (`x` → a file,
  then a regular file `x`) — the write-through-a-link trick, in both shapes;
- **duplicate member names**, which is how that trick gets smuggled past a
  reader that only looks at the last member;
- names that differ **only by case**, when the target filesystem folds case
  (APFS does) — both cannot exist, and quietly storing one file's bytes under
  the other's name is corruption.

A refusal happens before extraction starts, so nothing is half-written, and
nothing is ever created outside the target directory. An encrypted archive goes
through the same audit.

## First boot on the lodge DB, and how to go back

Take a backup before upgrading to a release with `lodge.db`, with the release
you are leaving: `woltspace backup --tag pre-upgrade`.

The first boot imports the old state files into `.space/lodge.db` and renames
each one it read to `<name>.imported`: `<wolt>/.state/sessions/` (a folder),
`.space/auto-grants.json`, `.space/digs/grants.json`, `.space/wolf/` stamps
and `jobs.jsonl`, `.space/telegram/chats/*.json`, the slack files under
`<wolt>/.state/chat/slack/`, `.space/apps/*.json`. The app logs stay. A store
that fails to import keeps its files, and the next boot tries it again. A
single session or app file that isn't valid JSON is skipped and logged, not
retried: it stays only in the renamed `.imported` folder or file, to fix by
hand.

The lodge answers `/health` with 200 either way, so read readiness from the
body: `db.error` (the file cannot be opened) and `db.import_error` (one entry
per store that failed) must both be absent, and `db.imported` names the
stores that were imported.

To go back to the older release:

```bash
woltspace stop
# install the older release, then put the old files back:
cd "${WOLTSPACE_WOLTS_DIR:?set it to the lodge data root}"
find . -maxdepth 5 \( -path './*/.state/*' -o -path './.space/*' -o -path './.archive/*' \) \
     \( -name '*.imported' -o -name '*.imported.[0-9]*' \) -prune -print | sort -rV |
while read -r p; do b=${p%.imported*}; [ -e "$b" ] || mv "$p" "$b"; done
# set lodge.db aside, so a later upgrade imports the files as they are then:
t=$(date +%Y%m%d-%H%M%S)
for f in .space/lodge.db .space/lodge.db-wal .space/lodge.db-shm; do
  if [ -e "$f" ]; then mv "$f" "$f.rolled-back-$t"; fi
done
woltspace start
```

A file imported more than once (upgrade, downgrade, upgrade again) left
`<name>.imported.2`, `.3`, ...; the newest one is put back.

The older release reads the files and never `lodge.db`, so everything written
after the import (new sessions, grants, cron runs, chat routing) is lost on
the way back; restore the `pre-upgrade` backup instead if the lodge ran for a
while.

Set `lodge.db` aside as above, every time. If it stays, upgrading again keeps
its rows and only adds new ones from the files: a grant revoked on the older
release would come back, and a session's newer status would be ignored.

Cron runs made on the newer release are recorded only in `lodge.db`. When the
older release starts, its catch-up looks back 24 hours, so a recurring cron
with catch-up on that ran in that window can run once more.

## Running colonies

No locks are taken, so a backup of a live lodge is safe, and `lodge.db` is
snapshotted read-only. A quiescent colony — sessions finished, nothing
mid-write — gives a tidier snapshot, but nothing breaks if you do not wait.

On Docker Desktop (macOS, Windows) the wolts folder is a bind mount across a
VM boundary, and SQLite's WAL is not safe across it: never open `lodge.db`
from the host, not even to read it. Read it inside the container, as the
lodge's user: `docker exec -u node <container> woltspace db show <table>`.
Back up inside it too and copy the archive out: `docker exec -u node
<container> woltspace backup --tag <tag>` writes to `/workspace`, which is not
the mount, so `docker cp <container>:/workspace/woltspace-backup-<tag>.tar.gz .`;
for an encrypted one the container needs `-e WOLTSPACE_BACKUP_AGE_RECIPIENT`.

## Off the machine: a bucket (Cloudflare R2)

`woltspace backup` writes files; getting them off the machine is `rclone`'s
job. No upload code in woltspace, no new setting. One bucket per owner, one
prefix per lodge:

```
<bucket>/lodges/<lodge>/latest.json                               a hint: tag, sha256, version, time
<bucket>/lodges/<lodge>/snapshots/woltspace-backup-<tag>.tar.gz.age
```

Only encrypted backups go to the bucket: the upload script below refuses
anything else. The guided setup is the `setup-backup` platform skill.

**Two tokens.** R2 has no write-only token, so "the lodge cannot erase its own
history" comes from a token scoped to one bucket plus a bucket lock:

1. Create the bucket. Then, in R2 → API tokens, create two tokens, both scoped
   to that bucket only: **Object Read & Write** for the lodge, **Object Read
   only** for restores. Keep the read-only one off the lodge.
2. Lock the snapshots, not `latest.json` (it is rewritten every day):

   ```bash
   npx wrangler r2 bucket lock add <bucket> keep-snapshots lodges/<lodge>/snapshots/ --retention-days 90
   ```

   Inside the retention window nothing can overwrite or delete a snapshot, with
   any token, so a compromised lodge cannot eat its backups.
3. On the lodge, an `rclone` remote with the read & write token
   (`rclone config file` shows where it lives; the token stays in rclone's
   config, not in `.env`):

   ```ini
   [r2]
   type = s3
   provider = Cloudflare
   access_key_id = <read & write key id>
   secret_access_key = <read & write secret>
   endpoint = https://<account id>.r2.cloudflarestorage.com
   acl = private
   no_check_bucket = true
   ```

**The daily upload.** A small script, say `~/bin/backup-to-r2.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
umask 077   # owner-only, like everything woltspace backup writes
: "${WOLTSPACE_BACKUP_AGE_RECIPIENT:?is not set: nothing leaves this machine unencrypted}"
out=~/.woltspace/backups
dest="r2:<bucket>/lodges/<lodge>"
tag=$(date -u +%Y%m%d-%H%M%S)
woltspace backup --tag "$tag" --out "$out"
file=woltspace-backup-$tag.tar.gz.age
if [ ! -f "$out/$file" ]; then
  echo "no $file: refusing to upload an unencrypted backup" >&2; exit 1
fi
rclone copyto "$out/$file" "$dest/snapshots/$file" --immutable
sum=$(sha256sum "$out/$file" | cut -d' ' -f1)
printf '{"tag":"%s","sha256":"%s","version":"%s","taken_at":"%s"}\n' \
  "$tag" "$sum" "$(woltspace --version | awk '{print $NF}')" "$(date -u +%FT%TZ)" > "$out/latest.json"
rclone copyto "$out/latest.json" "$dest/latest.json"
echo "backup $tag uploaded"
```

The two refusals are the point: without a recipient, or if the file is not a
`.tar.gz.age`, nothing is uploaded. It is run by the machine's own scheduler: no agent, no tokens. A crontab line
(`crontab -e`; cron has a bare `PATH`, so name the directory `woltspace`,
`rclone` and `age` live in):

```
30 3 * * * PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin WOLTSPACE_BACKUP_AGE_RECIPIENT=age1... bash $HOME/bin/backup-to-r2.sh >> $HOME/.woltspace/backups/upload.log 2>&1
```

or a systemd user timer:

```ini
# ~/.config/systemd/user/lodge-backup.service
[Service]
Type=oneshot
Environment=WOLTSPACE_BACKUP_AGE_RECIPIENT=age1...
ExecStart=/bin/bash %h/bin/backup-to-r2.sh

# ~/.config/systemd/user/lodge-backup.timer
[Timer]
OnCalendar=*-*-* 03:30
Persistent=true

[Install]
WantedBy=timers.target
```

then `systemctl --user enable --now lodge-backup.timer`. If you would rather
see the backup on the lodge's wolves page, a wolf cron can run the same script
instead, at the price of an agent session (and its tokens) every night:
`woltspace wolf add --cron '30 3 * * *' --message 'Run bash ~/bin/backup-to-r2.sh and reply with its last line.'`.

`--immutable` refuses to replace a snapshot that is already there. `woltspace
backup` itself creates the archive owner-only (0600) whatever the umask; the
`umask` line covers `latest.json` and the folder. Old local copies in
`~/.woltspace/backups` are yours to prune.

**Restore from the bucket**, on a machine where your age key is, with the
read-only token in an `rclone` remote of its own:

```bash
src="r2-ro:<bucket>/lodges/<lodge>"
# The newest snapshot by its own name. Tags are UTC timestamps, so they sort.
newest=$(rclone lsf "$src/snapshots" --include 'woltspace-backup-*.tar.gz.age' | sort | tail -1)
# latest.json is only a hint: it sits outside the lock and the lodge rewrites it.
rclone cat "$src/latest.json"; echo "restoring $newest"
rclone copyto "$src/snapshots/$newest" "$newest"
woltspace restore "$newest" --identity ~/lodge-backup-key.txt --to ~/lodge-restored
python3 -c 'import json,sys; m=json.load(open(sys.argv[1])); print(m["tag"], m["created_at"], m["woltspace_version"])' \
  ~/lodge-restored/backup-manifest.json
WOLTSPACE_WOLTS_DIR=~/lodge-restored/wolts woltspace start
```

Pick the snapshot from the `snapshots/` listing, not from `latest.json`: the
lock covers `snapshots/` only, so a lodge that went wrong can rewrite
`latest.json` to point at an old snapshot (or at a new one it uploaded; the
lock stops overwrites, not new objects). Before you boot, check that the
manifest's `tag`, `created_at` and `woltspace_version` are the snapshot you
meant: a date far older than the tag, or a version you never ran, means stop.

### The restore checks

A backup nobody has restored is a rumour too. The lodge cannot restore its own
backups (it has no key, by design), so the check comes in two halves.

**Weekly, on the lodge, no key.** A script beside the upload, scheduled the
same way. It downloads the newest snapshot and checks everything that needs no
key: `latest.json` is less than 48 hours old (a nightly upload that silently
stopped is the likeliest real failure), the newest name in `snapshots/` is the
one `latest.json` names, the sha256 matches, the file is an age file, and its
size is sane.

```bash
#!/usr/bin/env bash
set -euo pipefail
umask 077
src="r2:<bucket>/lodges/<lodge>"
min_bytes=1000000   # about half your usual snapshot size
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
rclone copyto "$src/latest.json" "$work/latest.json"
field() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$work/latest.json" "$1"; }
tag=$(field tag); sum=$(field sha256)
fail() { echo "backup check FAILED: $*" >&2; exit 1; }
hours=$(python3 -c 'import datetime as d,sys; t=d.datetime.fromisoformat(sys.argv[1].replace("Z","+00:00")); print(int((d.datetime.now(d.timezone.utc)-t).total_seconds()//3600))' "$(field taken_at)")
[ "$hours" -lt 48 ] || fail "the newest upload is $hours hours old: is the nightly upload still running?"
newest=$(rclone lsf "$src/snapshots" --include 'woltspace-backup-*.tar.gz.age' | sort | tail -1)
[ "$newest" = "woltspace-backup-$tag.tar.gz.age" ] || fail "latest.json names $tag, the newest snapshot is $newest"
rclone copyto "$src/snapshots/$newest" "$work/snapshot"
[ "$(sha256sum "$work/snapshot" | cut -d' ' -f1)" = "$sum" ] || fail "sha256 of $newest does not match latest.json"
[ "$(head -n1 "$work/snapshot")" = "age-encryption.org/v1" ] || fail "$newest is not an age file"
[ "$(stat -c%s "$work/snapshot")" -ge "$min_bytes" ] || fail "$newest is smaller than $min_bytes bytes"
echo "backup check ok: $newest"
```

**Monthly, by hand, on another machine where your key is.** The full
restore-and-boot, on a scratch directory and a spare port, never over the live
lodge:

1. Run "Restore from the bucket" above with `--to /tmp/restore-check`.
2. **Delete every restored secret, then boot with the wolf and the chat
   connectors off.** The `.env` files and `config.json` hold the live tokens: a
   scratch lodge with them would start a second Telegram/Slack poller on the
   same bot (your messages could land there instead of the real lodge), and an
   app's `.env` could reach a live service. With the wolf on, the real crons
   would fire.

   ```bash
   r=/tmp/restore-check/wolts
   find "$r" -name '.env*' -not -name '*.example' -not -name '*.sample' -delete
   rm -f "$r/.space/platform/config.json"
   left=$(find "$r" \( -name '.env*' -not -name '*.example' -not -name '*.sample' \) -o -path "$r/.space/platform/config.json")
   if [ -n "$left" ]; then
     echo "secrets in the scratch tree, refusing to boot: $left" >&2; exit 1
   fi
   WOLTSPACE_WOLTS_DIR=$r WOLTSPACE_PUBLIC_TUNNEL=false \
     ENABLE_TELEGRAM_BOT=false ENABLE_SLACK_BOT=false WOLTSPACE_WOLF=false WOLTSPACE_VULTURE=false \
     woltspace serve --port 7778
   ```

   It takes its own control-plane lock, so it runs beside a lodge on the same
   machine.
3. Check `curl -s localhost:7778/health` is ok, the session count from
   `curl -s localhost:7778/sessions` equals `lodge_db.tables.sessions` in the
   restored `backup-manifest.json`, and the wolts on disk match its `wolts` list.
4. Stop it and `rm -rf /tmp/restore-check`.

## The container era

`woltspace backup [tag] [--bundle]` in the bash launcher is the older shape:
`docker commit` of the running container, `rsync -a` of the whole wolts
directory, optionally zipped around a `docker save` image tar. It predates
native mode — there is no container to commit on a native install, and the bare
copy carried every `node_modules` and virtualenv along with the data. It still
works for container users and this change does not alter it; it now points at
the native command when the wheel is installed.

## Restoring across filesystems

When moving backups across filesystems, an archive with names differing only by
case (for example `Notes.md` and `notes.md`) restores both on a case-sensitive
target. On a case-insensitive target, restore refuses those collisions with an
error before extraction. This includes typical macOS volumes; the actual target
filesystem determines the behavior.
