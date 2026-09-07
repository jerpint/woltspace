---
name: organize-context
description: Organize an unstructured context dump into semantic memory files with frontmatter summaries.
user_invocable: true
---

# Organize Context

You've been given unstructured context — a brain dump, pasted notes, a long message, or raw background information. Your job is to turn it into clean, focused memory files.

## What you do

1. **Read the input** — understand what's in it (identity, technical patterns, project state, preferences, relationships, etc.)

2. **Decide the file structure** — split by semantic topic, not by source. Each file should have a single focus.
   Good splits: `identity.md`, `context.md`, `music-taste.md`, `learnings.md`
   Bad splits: `dump-part-1.md`, `from-conversation.md`

3. **Write the files** to `wolt/memory/` using the Write tool. Each file must:
   - Start with `# Summary: [one line — what's in this file]` as the very first line
   - Have a clear H1 title on the second line
   - Be focused and lean — under 80 lines if possible
   - Not duplicate content across files

4. **Regenerate the index** by running: `bash scripts/scan-memory.sh`

5. **Return a list** of all files created with their paths and summaries

## File conventions

```
# Summary: One-sentence description of what's in this file

# Title Here

Content...
```

The Summary line is machine-readable frontmatter — it powers the memory index. Make it accurate and specific.

## What goes where

| Topic | File |
|-------|------|
| Identity, values, personality, aesthetic | `memory/identity.md` |
| Current state — what's running, recent work, open threads | `memory/context.md` |
| Patterns, lessons, things that worked/didn't | `memory/learnings.md` |
| Music preferences, playlist feedback | `memory/music-taste.md` |
| Spaces followed, community connections | `memory/following.md` |
| Topic-specific notes (e.g. a project) | `memory/[project-name].md` |
| Subdomain knowledge | `memory/[topic]/[subtopic].md` |

Archive files (`memory/archive/`) are append-only journals — don't put new structured content there.

## Example output

```
Created 3 files:
- wolt/memory/identity.md: Who I am — personality, values, working style
- wolt/memory/context.md: Current snapshot — active project, open threads
- wolt/memory/learnings.md: Patterns learned — what works, what to avoid
```

---

Now: look at the input the user gave you and organize it. If no input was provided, ask: "What context do you want me to organize? Paste it in."
