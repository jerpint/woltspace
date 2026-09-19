# Colony seeds

A colony seed is a shareable starter, not a backup. It is designed to fit in
an ordinary Git repository and to install as fresh, independently owned wolts
and apps.

“Seed” describes the deliberately shareable data boundary, not repository
visibility. Start in a private repository, test with trusted recipients, and
only make that repository public after reviewing every authored identity and
rule. Woltspace never creates or changes the repository's visibility.

```sh
woltspace seed create ./my-seed \
  --name my-seed \
  --wolt scribe \
  --wolt bloggo \
  --app scribe \
  --app blog

woltspace seed inspect ./my-seed
git -C ./my-seed init

# On another machine:
woltspace seed install https://github.com/example/my-seed.git
```

`seed create` refuses an existing output path. It writes a deterministic
`seed.json`, a readable README, and only the following allowlisted material:

- each selected wolt's portable `wolt.json` fields, `identity.md`, and the
  human-authored portion of `CLAUDE.md`;
- user-owned skills selected explicitly with `--skill WOLT:SKILL`;
- selected app source committed in a clean, standalone Git repository, including
  its `woltspace.json`; or,
  when the app has a credential-free HTTPS origin, its URL and exact commit
  SHA. Private origins work when the receiving machine has access.

Machine-selected harnesses and models are omitted. So are sessions,
transcripts, context, learnings, archives, drafts, sparks, sites, application
data, lodge state, credentials, worktrees, dependencies, caches, builds, Git
internals, ports, and public tunnel state. Platform-managed rules and skills
are also omitted because the receiving Woltspace install supplies its own
current copies.

The seed creator rejects secret-shaped paths, credential-like content,
machine-specific home paths, symlinks, files over 5 MiB, and packages over
50 MiB. It also rejects apps with uncommitted tracked changes so executable
configuration and source always come from the same commit. This is a safety
boundary, not a substitute for reviewing the small result before publishing:
authored identity and rules can intentionally name
people, organizations, URLs, or other shareable details that software cannot
classify for you.

Ask a wolt to run the bundled `woltspace-seed-review` skill before the first
push or any visibility change. It pairs the structural CLI inspection with a
semantic review of the deliberately selected content and reports blocked,
owner-confirmation, or ready without granting itself permission to publish.

## Installation semantics

`install` validates the whole package and checks every destination name before
writing. It refuses to overwrite an existing wolt or app. Installed wolts get:

- `origin: starter`, so they do not masquerade as a user-created first wolt;
- provenance pointing back to the colony source (and Git revision for a remote
  colony);
- the receiving installation's current platform-managed rules;
- the published identity, authored rules, and explicitly selected skills;
- fresh, empty context and learnings.

Apps are private and stopped after install. Ports are assigned from the
receiving lodge's available range. Bundled source is copied; pinned HTTPS Git
apps are cloned and checked out at the recorded commit. Dependencies and app
data remain derived local state and are not included in the colony repository.

Installation rolls back paths it created after ordinary failures and Ctrl-C.
Like most filesystem installers, it cannot promise atomicity across sudden
process or machine death. A later retry refuses any names left behind instead
of overwriting them; inspect and remove only those partial starter directories
before retrying.

## Seed or backup?

These are separate safety lanes, not modes of one export command:

| Need | Use | Contains | Intended home |
| --- | --- | --- | --- |
| Share or recreate a starter colony | `woltspace seed create` | Selected identity, rules, skills, and app source | A reviewable private or public Git repository |
| Recover this lived-in lodge | `woltspace backup` | Stateful history, owner data, sessions, and unique work according to backup policy | A private backup archive |

Install a seed with `woltspace seed install`; recover a backup with
`woltspace restore`. There is no flag that silently turns one into the other.

Updates are deliberately outside the v1 contract. An installed starter is an
independent copy: a later upstream version must never overwrite its lived
memory, data, or customization without a separate reviewed update design.
