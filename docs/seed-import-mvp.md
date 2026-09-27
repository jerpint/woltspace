# Seed import MVP contract

Status: proposed gate for the first experimental implementation.

This document narrows Colony Seed v1 into an anytime import primitive for a
running lodge. It does not install or update Woltspace. It does not apply seed
updates.

## First slice

The first slice provides:

1. read-only inspection of a local seed or an exact Git revision;
2. selection of one or more wolts, with required keeper/app closure;
3. import into an existing lodge with collision refusal;
4. one durable receipt and immutable base snapshot per import;
5. read-only source status and component diffs.

The slice excludes rename, fork/detach commands, background checks, update
proposal, update apply/rollback, plugins, catalog UI, DMG/default-seed work, and
website work.

## Source identity

A remote source is entered as a credential-free HTTPS Git URL plus an explicit
`--ref`. SSH, userinfo, query strings, fragments, and the ambiguous `URL@REF`
form are not accepted. The implementation resolves the ref to a full commit
before inspection or import.

The source record contains:

```json
{
  "url": "https://github.com/example/starter-wolts.git",
  "requested_ref": "v0.1.0",
  "resolved_commit": "40 lowercase hex characters",
  "seed_digest": "64 lowercase hex characters"
}
```

A full commit is immutable install identity. A tag is accepted after resolving
to a commit. A branch is refused for import in the first slice. Local-directory imports have `url`, `requested_ref`, and
`resolved_commit` set to null while retaining the seed digest.

Remote inspection clones with no checkout into a private temporary directory,
reads the resolved tree directly from Git objects, validates the entire seed
before presenting it, and removes the clone afterward. Every Git subprocess
sets `GIT_ALLOW_PROTOCOL=https`, `GIT_TERMINAL_PROMPT=0`, and
`GIT_LFS_SKIP_SMUDGE=1`. No global Git protocol/helper configuration may expand
the allowed source behavior, and no seed content is executed during inspect or
import.

## Deterministic seed digest

The digest is SHA-256 over every regular package file except paths beneath
`.git/`, in ascending POSIX-byte path order. For each file, hash:

```text
UTF-8(relative POSIX path) + NUL + ASCII(git mode) + NUL + exact file bytes
```

Allowed modes are `100644` and `100755`; executable state is therefore part of
the digest. No newline or Unicode normalization occurs. Remote inspection uses
`git ls-tree -r -z` plus `git cat-file blob` against the resolved commit, never
a working-tree checkout, so attributes, filters, LFS smudge, and `core.autocrlf`
cannot change validated bytes. Symlinks, Git submodules/gitlinks (`160000`), LFS
pointer blobs, and every other mode are invalid.

Local-directory inspection hashes filesystem bytes plus normalized
`100644`/`100755` mode. If the directory is a Git repository with uncommitted or
untracked package files, inspection warns that its digest may not match the
pushed revision.

## Stable component identity and selection

Each manifest entry gains an additive stable `id`:

```json
{"id": "wolt:scribe", "name": "scribe", "skills": ["editorial"]}
{"id": "app:scribe", "name": "scribe", "keeper": "wolt:scribe"}
```

For old v1 seeds without `id`, inspection derives `wolt:<name>` and
`app:<name>`. IDs match `^(wolt|app):[a-z0-9][a-z0-9_-]{0,63}$` and are unique
within a seed. They are not local filesystem names.

Selection names stable IDs. Selecting an app selects its keeper wolt. Selecting
a wolt does not automatically select every app it keeps. The inspection result
shows requested and required components separately before import.

The first slice installs each component under its manifest `name`. Any occupied
destination refuses the whole import before writes. Rename mapping is recorded
as an empty object in the receipt but has no MVP UX.

## Portable and owned paths

The immutable base snapshot contains only selected, validated seed material:

```text
base/
  seed.json
  wolts/<name>/wolt.json
  wolts/<name>/identity.md
  wolts/<name>/rules.md
  wolts/<name>/skills/<skill>/**
  apps/<name>/**              # bundled source, or app.json Git reference
```

`README.md` and unselected components are not part of the base snapshot.
`base/seed.json` is a canonical selected-component projection with the original
seed name/format and stable IDs. Its bytes need not equal the source manifest.
`base/seed.json` and every projected `base/wolts/*/wolt.json` use the same
canonical JSON serializer as the receipt. The projected wolt config is the
source portable object with only the allowed portable fields; it never contains
the installed `seed` awareness block, `origin`, or legacy provenance. The base
digest therefore depends only on the imported portable definition, not on local
installation metadata or serializer drift.

The receipt maps every seed-owned source to its installed representation.
Platform-managed skills and managed `CLAUDE.md` content are never seed-owned. A
selected skill directory is owned by its stable wolt component ID. Ownership
entries have this shape:

```json
{"seed_path":"rules.md","local_path":"CLAUDE.md","mode":"region"}
{"seed_path":"skills/editorial/**","local_path":".claude/skills/editorial/**","mode":"file"}
{"seed_path":"identity.md","local_path":"wolt/memory/identity.md","mode":"manual-review"}
{"seed_path":"wolt.json","local_path":"wolt/wolt.json","mode":"fields","fields":["name","type","role","capabilities","description"]}
```

The `region` entry means the authored content outside the existing
`<!-- WOLTSPACE:BEGIN ... -->` through `<!-- WOLTSPACE:END -->` managed block,
the same boundary `_managed_rules` enforces. App ownership entries map bundled
files or the pinned `app.json` reference to the installed app paths.

`identity.md` is copied at initial import, but it is writable lived identity
afterward. Read-only status may display an upstream identity change; it never
classifies that change as safely mergeable or mutates local identity.

## State layout

Lodge-owned state lives under:

```text
<wolts-dir>/.space/seeds/
  installs/<install-id>/
    receipt.json
    base/
```

`install-id` is a random UUIDv4 encoded as lowercase canonical text. Directories
and files are owner-only (`0700` directories, `0600` receipt/base files where
the host permits POSIX modes).

Each imported wolt's `wolt.json` contains a compact source-awareness block:

```json
{
  "seed": {
    "install_id": "uuid",
    "component_id": "wolt:scribe",
    "source_url": "https://github.com/example/starter-wolts.git",
    "requested_ref": "v0.1.0",
    "resolved_commit": "40 lowercase hex characters",
    "digest": "64 lowercase hex characters",
    "tracking_ref": "v0.1.0"
  }
}
```

This lets the wolt understand and discuss its own upstream without first
discovering lodge-internal state. The block is a read-only mirror maintained by
the package-owned import/update operation; the lodge receipt remains
authoritative because one import may own several wolts and apps that advance as
one seed revision. The receipt path is derived from `install_id`, never accepted
from seed content.

The importer owns all lineage. Export drops `seed`, `provenance`, and `origin`
from portable `wolt.json`; import ignores/rejects those keys if hostile seed
content supplies them, then writes `origin: starter` plus the `seed` block
itself. New imports do not write the older `provenance` mirror, avoiding two
lineage records that can disagree. Read-only status trusts the lodge receipt and
warns if a wolt's manifest mirror is missing or differs.

Local-directory imports set the source/ref/commit/tracking values to null but
still record install ID, component ID, and digest. Apps retain their existing
private/stopped source metadata; their shared lineage lives in the receipt.

## Receipt schema

`receipt.json` is canonical JSON (UTF-8, sorted keys, two-space indentation,
single trailing newline):

```json
{
  "format": "woltspace.seed-install/v1",
  "install_id": "uuid",
  "installed_at": "UTC RFC3339 seconds ending in Z",
  "installer_version": "Woltspace version",
  "seed": {
    "format": "woltspace.colony-seed/v1",
    "name": "starter-team",
    "digest": "sha256",
    "base_digest": "sha256 of base using the seed digest algorithm"
  },
  "source": {
    "url": "https URL or null",
    "requested_ref": "tag/commit or null",
    "resolved_commit": "full commit or null",
    "tracking_ref": "ref or null"
  },
  "components": [
    {
      "id": "wolt:scribe",
      "kind": "wolt",
      "source_name": "scribe",
      "local_name": "scribe",
      "owned_paths": [
        {"seed_path":"identity.md","local_path":"wolt/memory/identity.md","mode":"manual-review"},
        {"seed_path":"rules.md","local_path":"CLAUDE.md","mode":"region"},
        {"seed_path":"skills/editorial/**","local_path":".claude/skills/editorial/**","mode":"file"},
        {"seed_path":"wolt.json","local_path":"wolt/wolt.json","mode":"fields","fields":["name","type","role","capabilities","description"]}
      ]
    }
  ],
  "rename_map": {},
  "forked_from": null
}
```

Component records sort by stable ID; owned paths sort by seed path.
`tracking_ref` is null in the first slice unless a later explicit channel is
configured. Tracking is metadata for an owner-requested status check, never a
background poll or authorization to mutate.

The receipt additionally records `invoked_by`: `{ "kind": "wolt", "wolt":
"...", "session": "..." }` when Woltspace session environment identifies an
initiator, otherwise `{ "kind": "cli" }`. This is audit attribution, not proof
of owner approval.

## Transaction and failure behavior

Import takes a lodge-level seed-import lock around validation through publish.
It validates source, selection closure, every destination, and the complete
receipt/base snapshot before modifying the lodge. Components and seed state are
staged beneath the lodge filesystem. The operation then moves components into
place and publishes the completed install directory last.

On ordinary exception or interruption, every component moved by this operation
and all staged seed state are removed. Existing paths are never changed. A
published receipt therefore means the import completed. Sudden machine death is
outside the v1 atomicity claim; a later recovery design can use the absent
receipt plus the imported wolt manifest's `seed.install_id` pointer to diagnose
an interrupted import.

If a moved wolt becomes referenced by a live session registry entry during the
narrow publish/rollback window, rollback refuses to delete that wolt and reports
manual recovery instead of removing a live session's working directory.

After success, the ordinary dynamic wolt/app discovery paths must see the new
components on their next read. The operation must not restart the control plane,
start a session, start an app, or expose an app publicly.

## Inspection and consent output

Read-only inspection returns structured data and human output containing:

- source URL/ref, resolved commit, and digest;
- every component and dependency added by closure;
- full authored rule text;
- identity text, labelled as initial identity;
- every skill file path, explicitly marking scripts/executable files;
- app distribution, source URL/commit or bundled file list;
- the statement: “Wolt rules and skills become instructions followed with the
  wolt's local access. Apps and skill scripts are code that may run on this
  machine.”

Do not call a seed “verified.” Structural validation does not establish safety.

For this CLI-first experiment, import is an owner-invoked mutation. An existing
wolt may prepare inspection and selection, but the first slice does not claim a
terminal prompt is cryptographic proof of a human. The later lodge UI will mint
a one-use human approval for import and update apply. No `--yes` flag is treated
as transferable authority.

## Read-only status

Status loads the local receipt/base without requiring the remote to exist. With
no `--to`, a GitHub-style semver tag check uses `git ls-remote --tags`, lists
tags newer than the requested installed tag, and writes nothing. The owner (or
preparing wolt) chooses an exact candidate with `seed status ... --to <tag|sha>`;
status resolves and validates it, then reports component additions/removals and
exact portable-file diffs for installed component IDs. It writes nothing.

Identity changes are labelled `manual-review`. Skill scripts and app changes
are labelled `executable`. Changes to portable `wolt.json` fields are labelled
`configuration`. Rules text may produce a non-blocking hint when it names an
unselected seed component, because semantic dependencies cannot be inferred
reliably. A missing/deleted/rewritten remote ref is reported without invalidating
the local receipt or base.

## Acceptance gate

The first slice is complete when tests prove:

1. exact tag/commit inspection is read-only and uses committed bytes;
2. selection closure is explicit and collisions cause zero writes;
3. import into a running disposable lodge creates fresh stopped/private
   components visible to normal discovery without restart;
4. receipt and base are complete, private, deterministic, and published last;
   every imported wolt manifest exposes the matching compact source-awareness
   block;
5. ordinary failure and Ctrl-C remove every newly created path;
6. deleting the upstream repository does not prevent local base/status reads;
7. status lists a semver-newer tag, and explicit `--to <tag>` produces a
   read-only component/file diff;
8. moving branches, digest mismatches, hostile paths, secrets, symlinks, and
   malformed component IDs fail closed;
9. no memory, sessions, credentials, runtime state, public exposure, or update
   mutation crosses the boundary.
