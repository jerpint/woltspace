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

## What goes in

Everything under `WOLTS_DIR` that is data:

- each wolt's `wolt/` tree — `memory/`, `site/`, `sparks/`, `drafts/`, `apps/`
- `CLAUDE.md` and `wolt.json` per wolt
- `woltspace.json` (but not `.env` — see "Secrets are not backed up")
- `.state/` (session registry, wolf state) and `.space/` (platform metadata)
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

`.local/share/claude` is the single biggest thing in a lived-in colony —
**12.8GB** of redownloadable CLI binaries across per-wolt HOMEs on the colony
these rules were measured against. It is scoped by path deliberately: a wolt's
own directory called `claude` is data, the rest of `.local` is data, and
`.local/share/opencode` (session state, easily 150MB) is kept. Conversation
transcripts live in `.claude/projects`, which is also kept.

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

There is no `--with-secrets`. The safe default is the only default.

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

1. **At the top level of `WOLTS_DIR`, nothing is excluded by name** except what
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

## Trust but verify

A backup nobody has opened is a rumour. Every run re-opens the archive it just
wrote, parses the manifest back out of it, checks the member count against what
the manifest claims, and spot-reads one `wolt.json`. A mismatch is an error, not
a warning.

Unreadable files are the opposite: they warn, they are listed in the summary and
recorded in the manifest, and the backup still completes.

## Restore

```bash
woltspace restore woltspace-backup-pre-rc5.tar.gz [--to DIR]
```

Restore extracts into a **new** directory — by default
`<archive-stem>-restored` beside the archive — and refuses to write into an
existing non-empty one. There is no `--force`. The archive's tree lands at
`<target>/wolts/`, and the command prints the exact line to boot from it:

```
boot from it with:
  WOLTS_DIR=/path/to/woltspace-backup-pre-rc5-restored/wolts woltspace start
```

Pointing a colony at a restored directory is the human's explicit act, never a
side effect of unpacking a file.

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
nothing is ever created outside the target directory.

## Running colonies

No locks are taken, so a backup of a live lodge is safe. A quiescent colony —
sessions finished, nothing mid-write — gives a tidier registry snapshot, but
nothing breaks if you do not wait.

## The container era

`woltspace backup [tag] [--bundle]` in the bash launcher is the older shape:
`docker commit` of the running container, `rsync -a` of the whole wolts
directory, optionally zipped around a `docker save` image tar. It predates
native mode — there is no container to commit on a native install, and the bare
copy carried every `node_modules` and virtualenv along with the data. It still
works for container users and this change does not alter it; it now points at
the native command when the wheel is installed.
