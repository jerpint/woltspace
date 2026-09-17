# Shared lodge skills

Teach a local workflow once and make it available to every wolt in your lodge.
Shared skills live in your lodge data, alongside your wolts, rather than inside
Woltspace's installed package. They are local to this lodge: Woltspace does not
publish, upload, fetch or distribute them.

Use the Woltspace-owned `lodge-skills` helper for setup and troubleshooting.
On copy delivery it is named `woltspace-lodge-skills`; on plugin delivery it
uses the platform skill namespace for your harness.

## Add a skill

The source folder is `$WOLTSPACE_WOLTS_DIR/.space/shared-skills/` (normally
`~/.woltspace/wolts/.space/shared-skills/` on native installs and
`/workspace/wolts/.space/shared-skills/` in containers). Woltspace creates this
folder during skill refresh.

Each skill is a folder named `lodge-<name>` containing a standard `SKILL.md`:

```text
.space/shared-skills/
  lodge-github-practice/
    SKILL.md
    references/             # optional
    scripts/                # optional
```

```markdown
---
name: lodge-github-practice
description: Use this lodge's GitHub bot identity for commits and pull requests.
---

Follow the lodge's reviewed GitHub workflow and verify commit attribution.
```

The directory and frontmatter name must match. Use lowercase letters, numbers
and single dashes, starting with `lodge-`, with at most 64 characters total.
Bare names and single/double-quoted name values are supported. Invalid or missing
names are skipped with a warning; Woltspace never renames your skills or rewrites their workflow instructions.
Skill directories must be real local directories, not symlinks. `SKILL.md` must
remain inside its skill directory.

## Shared-edit notice

During refresh, Woltspace automatically adds a marked notice to each valid shared
`SKILL.md`, after the frontmatter. It identifies the shared source, tells wolts
not to edit through their local link, and explains that shared edits persist for
all linked wolts. The notice is maintained in the source so every link sees the
same warning; workflow instructions remain intact. It is not added to private
per-wolt skills. If the source cannot be updated, Woltspace warns rather than
claiming the notice was installed.

## Discovery and ownership

After installing a version with shared-skill support, lodge startup refreshes
links for all existing wolts as well as newly created ones. Woltspace refreshes
links at lodge startup, when creating a wolt, and before
launching or resuming a platform-managed agent session. A new session discovers
new skills without a lodge restart. Already running sessions can retain their
loaded skill lists and instructions; start a new session after changing skills.
Directly launching a harness outside Woltspace does not run this refresh.

Each skill gets a relative link in the wolt's `.claude/skills/`. Claude and
OpenCode use that path; Codex uses the existing `.agents/skills` bridge. If the
wolt owns a separate `.agents/skills` directory, links are added there too.
This works independently of platform copy/plugin skill delivery.

A real skill directory or unrelated symlink at the same name is a per-wolt
override and is preserved with a warning. In a separately owned `.agents/skills`
directory, that directory's override applies to harnesses using it. Remove the
override to receive the shared version on the next refresh.

For a private variant, copy the source into your wolt's own skill folder under a
name without `lodge-` (for example `lodge-github-practice` → `github-practice`).
Update its frontmatter `name` to match and remove the marked shared-edit notice.
Keep the shared link: the private variant is a separate skill, not an override.
Editing through the shared link changes the source for everyone. Existing
same-name overrides are preserved for compatibility, but new private variants
should use their own unprefixed names.

Removing or renaming a shared skill removes only Woltspace's matching links on
next refresh, including dangling links. Wolt-owned files are never deleted.
Platform upgrades update platform skills and leave shared workflow instructions
alone; shared refresh maintains only the marked notice.
The folder is part of your lodge data and normal data backups; “local” does not
exclude a backup you explicitly create.

Shared skill discovery does not grant permission to edit outside a wolt's own
workspace or access credentials. Lodge owners manage these shared files, and
agents still follow their existing workspace and consent rules.
