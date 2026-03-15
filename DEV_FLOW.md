# Developing Woltspace (from inside the container)

This is a dev clone of woltspace, living inside a wolt's `projects/` directory.
The mounted copy at `/workspace/woltspace/` is "production" — don't edit it directly.

## The model

```
/workspace/woltspace/              ← production (mounted, serves the tunnel)
wolt/projects/woltspace/           ← dev clone (you work here)
wolt/projects/woltspace-branches/  ← worktrees for parallel work
```

## Quick start: single task

```bash
cd wolt/projects/woltspace
git checkout -b feat/my-thing
# ... make changes ...
git add -A && git commit -m "feat: my thing"
git push -u origin feat/my-thing
gh pr create --title "feat: my thing" --body "..."
```

Then notify the human. They review, merge, and rebuild.

## Parallel work: worktrees

When multiple sessions need to work on woltspace simultaneously:

```bash
cd wolt/projects/woltspace

# Session A
git worktree add ../woltspace-branches/feat-digest -b feat/digest
# → works in wolt/projects/woltspace-branches/feat-digest/

# Session B
git worktree add ../woltspace-branches/fix-slack -b fix/slack-ack
# → works in wolt/projects/woltspace-branches/fix-slack/
```

Each worktree is a full copy on its own branch. No conflicts between sessions.

When done:
```bash
cd wolt/projects/woltspace-branches/feat-digest
git push -u origin feat/digest
gh pr create --draft --title "feat: digest page"
```

Cleanup after merge:
```bash
cd wolt/projects/woltspace
git worktree remove ../woltspace-branches/feat-digest
git branch -d feat/digest
```

## Branch model

Two branches:
- **`main`** — stable. What containers run by default. What users install.
- **`staging`** — the workshop. All development merges here first via PR.

PRs target **staging**. When staging is solid, a PR from staging → main ships to users.

## Deploying changes

Changes go through staging first, then to main:

1. PR gets merged to **staging** on GitHub
2. Test with `woltspace rebuild --dev` (builds from staging)
3. When ready to ship: PR from staging → main
4. Human runs `woltspace rebuild` (builds from main — the default)
5. Or: `cd /workspace/woltspace && git pull` for code-only changes in dev mode

The tunnel always serves the mounted copy. Dev clones are for development only.

## Running tests

Tests run against the dev clone, not production:

```bash
cd wolt/projects/woltspace
./test/run-tests.sh unit          # fast, no deps
./test/run-tests.sh              # full suite (needs server + tmux)
```

## Git setup

The clone uses the shared PAT from `wolts/.env` (GH_PAT_TOKEN) via credential helper.
Commits are authored as `woltspace <woltspace@users.noreply.github.com>`.

## For Haiku (bot routing)

When dispatching sessions to work on woltspace:
- Use `project=woltspace` to scope the session to this dev clone
- For parallel tasks, the session should create a worktree and work there
- Always push to a branch and create a PR — never commit to main directly
