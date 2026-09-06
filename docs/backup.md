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
- `.env`, `woltspace.json`
- `.state/` (session registry, wolf state) and `.space/` (platform metadata)
- `.git` directories — a wolt's history is data, so it is archived in full

Symlinks are archived **as symlinks** and never followed. The skill-delivery
links point into the installed wheel; following them would swallow the platform
a backup is not supposed to be backing up.

## What is excluded, and why

Dropped wherever they appear, at any depth:

| Excluded | Why |
| --- | --- |
| `node_modules`, `.venv`, `venv` | reinstallable from a lockfile |
| `__pycache__`, `*.pyc`, `.cache`, `.pytest_cache` | derived, per-machine |
| `dist`, `build`, `.next`, `target` | build products |
| `.claude/plugins/cache` | plugin download cache |
| `wolt/worktrees`, `.worktui` | throwaway checkouts; the branches live in git |
| `.DS_Store`, `*.sock`, sockets and fifos | noise, or meaningless once restored |

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

It never contains a secret's **value**. `.env` is inside the archive — that is
the point of a backup — but nothing reads its contents into the manifest or the
printed summary.

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
