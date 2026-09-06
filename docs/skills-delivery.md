# Skill delivery — how platform skills reach a wolt

Platform skills live once, in `container/skills/`, and ship inside the wheel. This
document is the intent record for how they reach each wolt, why there are currently two
mechanisms, and how the second one retires the first. If you are changing
`container/lib/skills_sync.py`, `platform_skill_invoke` in `container/lib/harnesses.py`,
or renaming a skill, read this first.

## The two delivery paths

The sweep (`sync_all_wolt_skills`, run at every control-plane start — native lifecycle
and container supervisor preflight alike) asks one question per wolt: what does
`wolt.json` say in `skills_delivery`?

**Copy (default, legacy).** No flag, or any value other than `"plugin"`. The skills are
copied into the wolt's `.claude/skills/` under their historical `woltspace-<name>` names.
The sources are named bare (`notify`, not `woltspace-notify`), so the copy path re-applies
the prefix to both the directory name and the frontmatter `name:` — the frontmatter
matters because codex and opencode name skills from frontmatter while claude names them
from the directory. This path is stage-then-rename with crash recovery and a lock; it is
the mechanism every existing colony runs and it must keep behaving identically until the
ratchet completes.

**Plugin (opt-in).** `"skills_delivery": "plugin"`. Nothing is copied:

- One symlink, `.claude/skills/woltspace -> <platform skills dir>`, plus the
  `.agents/skills -> ../.claude/skills` bridge. codex and opencode recurse through those
  links and see the whole tree; claude does not recurse nested directories, so the link
  is invisible to it.
- Claude instead gets the same directory as a plugin: the sweep merges two entries into
  the wolt's `.claude/settings.json` (`extraKnownMarketplaces` with a `directory` source,
  `enabledPlugins`), registers the marketplace, and runs `claude plugin install` — in
  that order, because the install command does not read settings written moments before.
- Stale copies from the old path are swept **only after delivery is confirmed**, and only
  the exact platform skill names — a wolt-owned skill in the same directory, whatever it
  is called, is never touched.
- Skill **content is read live from the source path** in all three harnesses (verified by
  editing a skill after install: the next session quotes the edit while claude's install
  cache still lacks it). Upgrading the wheel therefore upgrades every delivered skill
  with no re-delivery step.

## Naming: one skill, three spellings

The plugin namespaces skills; the harnesses disagree about what that means. The full
matrix, all verified live:

| harness  | copy path (legacy)      | plugin path          | why |
|----------|-------------------------|----------------------|-----|
| claude   | `/woltspace-notify`     | `/woltspace:notify`  | plugin namespace comes from claude's plugin system; skill name from the **directory** |
| codex    | `@woltspace-notify`     | `@woltspace:notify`  | codex names from **frontmatter** and synthesizes the namespace when it sees `.claude-plugin/` at the tree root |
| opencode | ` /woltspace-notify`    | ` /notify`           | opencode has no plugin concept at all — bare frontmatter names, recursive SKILL.md scan (leading space dodges its command palette) |

Nothing outside `harnesses.py` may spell an invocation by hand. Code refers to a platform
skill by its base name and formats it with `platform_skill_invoke(harness, name,
delivery)` — the delivery mode comes from the same `wolt.json` field the sweep reads, so
a wolt's boot prompt always matches the names its skills actually answer to. Prose (docs,
skill bodies) names skills neutrally ("the notify skill") for the same reason.

## Why it works natively (the HOME subtlety)

In container mode each wolt has its own HOME, and per-HOME delivery is the obvious story.
Natively, every session runs with the human's real HOME — yet delivery still targets the
wolt directory. That works because claude resolves **project-level** `.claude/`
(settings, plugins, skills) from the session's working directory, which is the wolt
directory; codex and opencode reach the same place through the `.agents/skills` bridge in
that directory. Do not "fix" delivery to target the real HOME: it would make every wolt
share one skill set and one plugin state.

## The ratchet, and its end state

The two paths are deliberate, temporary scaffolding — not a permanent dual system:

1. **Now:** copy is the default; nothing changes for anyone. The plugin path exists so
   individual wolts can opt in and prove it against real sessions, a restart, and a wheel
   upgrade. Rollback at any point is removing the flag and restarting — the sweep
   re-derives everything from `wolt.json` on every boot, holds no migration state, and
   converts a wolt in either direction. There is no migration script because there is
   nothing to migrate: delivery state is a pure function of the flag.
2. **Then:** once opted-in wolts have soaked, a release flips the default to plugin. The
   flag remains as an explicit opt-out for stragglers.
3. **End state:** the copy machinery — staging, renaming, locking, crash recovery, the
   prefix shim — is deleted. One delivery mechanism (symlink + plugin) plus the naming
   templates is the intended resting point, with less total machinery than the copy sync
   it replaces.

If you find yourself extending the copy path rather than deleting it, the ratchet has
stalled — ask why before adding to it.

## Pointers

- Sweep and both delivery paths: `container/lib/skills_sync.py` (module docstring has the
  operational detail).
- Invocation templates and `platform_skill_invoke`: `container/lib/harnesses.py`.
- Boot-prompt assembly and platform-skill detection: `container/lib/sessions.py`.
- Plugin manifests: `container/skills/.claude-plugin/`.
- Retired skills live in `container/legacy-skills/`, outside the plugin root, because
  codex's recursive scan would otherwise surface them.
