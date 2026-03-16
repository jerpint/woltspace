## Projects
Projects live in `wolt/projects/`. They're isolated workspaces for building things.

**When someone asks to build something** (app, tool, script, experiment): use `claude_code` with the `project` parameter set. Pick a short, descriptive name. The session runs scoped to that project directory.

**When someone asks to work on an existing project** ("fix my dashboard", "update the todo app"): call `list_projects` first to find it, then `claude_code` with `project` set to the matching name.

**When someone just wants to chat or do wolt-level work** (update memories, check on things, site changes): no project needed — run the session at the wolt root as usual.

The key question: does this request belong to a specific project? If yes, scope it. If not, don't.