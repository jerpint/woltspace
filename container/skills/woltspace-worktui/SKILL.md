---
name: woltspace-worktui
description: Manage git worktrees for parallel development. Use when you need to work on a branch in isolation — creating worktrees, listing them, checking sessions, or cleaning up after merges.
---

# Worktui — Worktree Manager

`wt` manages git worktrees so you can work on multiple branches in parallel, each in its own isolated directory. Worktrees persist at `/workspace/wolts/.worktui/` (survives container rebuilds).

## When to use it

- Working on a platform PR (woltspace) — create an isolated worktree instead of committing on the dev clone's main branch
- Parallel tasks — each task gets its own worktree, no conflicts
- Reviewing or testing a remote branch — `wt create <branch>` checks it out into a worktree
- Cleaning up after merges — `wt clean` removes stale worktrees

## CLI commands

```bash
wt list [--json]                  # List all worktrees
wt create <branch> [--pr]        # Create worktree (+ optional draft PR)
wt delete <branch> [--branch]    # Remove worktree (+ optional branch delete)
wt sessions [<branch>] [--json]  # Claude sessions for a worktree
wt status                        # Info about current worktree
wt clean [--dry-run]             # Remove all non-dirty worktrees
wt remote [--json]               # Remote branches without local worktrees
wt projects [--json]             # Registered projects
wt pr <branch>                   # Show PR URL
```

## Typical workflow

```bash
# 1. Create an isolated worktree for your branch
wt create nw/fix-something

# 2. You're now in the worktree directory — do your work
#    (edit files, run tests, etc.)

# 3. Commit, push, open PR
git add . && git commit -m "fix something"
git push -u origin nw/fix-something

# 4. After PR is merged, clean up
wt delete nw/fix-something --branch
```

## Notes

- Worktrees are created under `~/.worktui/<project>/<branch>/`
- `WORKTUI_DIR` env var controls the base directory (default: `/workspace/wolts/.worktui`)
- The `wt` function is a shell wrapper (sourced from `~/worktui/wt.sh`) — it handles `cd` into worktrees after creation
- `wt` with no arguments launches the interactive TUI (navigate with j/k, vim-style)
- All commands support `--json` for machine-readable output
