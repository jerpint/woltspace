---
name: lodge-skills
description: Explain, create, edit, or troubleshoot local lodge-wide shared skills with lodge-prefixed names, including setup, discovery, and per-wolt overrides.
---

# Shared lodge skills

A shared skill teaches one local workflow to all wolts in the lodge. Its source
lives in lodge data, not the installed Woltspace package. Woltspace links each
skill into wolt skill paths; it does not publish, download, or distribute them.
This helper is platform-owned; the skills it helps create are lodge-owned.

## Find and set up the source

Run `woltspace paths` and use its `wolts_dir`, including custom/native/container
layouts. The shared source folder is `<wolts_dir>/.space/shared-skills/`.
Never assume another lodge's path or the current working directory is the root.

For an explanation request, explain the setup without writing files. For a
creation/edit request, carry out the requested work within existing authorization
and workspace rules. This skill grants no additional filesystem permissions.
If the shared folder is outside the permitted write surface, draft the complete
skill in the wolt's own workspace and provide the owner an install command with
the resolved destination; do not silently place a private skill elsewhere.

Create one real directory per skill, containing a standard `SKILL.md`. Both the
directory and frontmatter `name` must be exactly `lodge-<name>`: lowercase letters,
numbers and single dashes, at most 64 characters including the prefix.
Do not invent a remote repository, marketplace, or platform release for it.

Example: `<wolts_dir>/.space/shared-skills/lodge-github-practice/SKILL.md`:

```markdown
---
name: lodge-github-practice
description: Apply this lodge's GitHub identity and review practices when preparing changes.
---

Use the lodge's configured bot identity for commits and authenticated GitHub work.
Verify author and committer before pushing and preserve human review requirements.
```

Write instructions for the human's actual workflow, with a precise description
of when to use it. Keep credentials out. Put supporting scripts/references inside
the skill directory when useful. Skill directories cannot be symlinks, and
`SKILL.md` must remain inside its own skill directory. Invalid/mismatched names
are warned about and skipped, never silently renamed.

## Shared-edit notice

Refresh automatically adds a marked notice after each shared skill’s frontmatter,
pointing at its shared source and explaining that edits persist for all linked
wolts. Woltspace maintains only that block, preserving workflow instructions.
Read a refresh warning as incomplete notice installation, not proof of success.
Private per-wolt skills do not receive this notice.

## Discover and verify

Links refresh during lodge startup, wolt creation, and before a platform-managed
agent launch/resume. Start a new session to discover added skills; do not restart
the lodge just to add one. Existing sessions may retain old skill lists/content.
Direct harness launches do not refresh links.

Check `<wolt>/.claude/skills/lodge-<name>` resolves to the intended source and that
its `SKILL.md` and supporting files are readable. Claude/OpenCode use this path;
Codex uses `.agents/skills`, normally bridged to it. Separately owned agent skill
directories receive their own links. Verify actual discovery in the target fresh
session before claiming success, especially when its workdir is outside the wolt.

A real same-name skill or unrelated symlink is a preserved per-wolt override.
To customize one wolt, replace its shared link with a local copy and remove the
marked shared-edit notice from that private copy first: editing
through a link edits the shared source for everyone. Removing an override lets
the next refresh deliver the shared version. Removal/rename of a shared source
removes only Woltspace-owned links on next refresh, not wolt-owned overrides.

Platform upgrades leave shared workflow instructions alone; the generated notice
is maintained during refresh. They remain part of lodge
data backups, which does not make them a distributed skill collection.
For details, read `docs/shared-skills.md` under `install_root` from `woltspace paths`.
