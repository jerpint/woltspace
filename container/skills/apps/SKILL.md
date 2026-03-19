---
name: apps
description: "DEPRECATED — use /projects instead. The /app/ routes have been removed."
---

# Apps — DEPRECATED

**Apps have been replaced by Projects.** The `/app/{name}/` routes no longer exist on the server.

## What to do instead

Use the `/projects` skill or `/new-project` to create and manage projects.

- Old: `wolt/apps/{name}/` with `app.json` → served at `/app/{name}/`
- New: `wolts/projects/{name}/` with `woltspace.json` → served at `/project/{name}/`

## Migrating an existing app

If you have an app in `wolt/apps/{name}/`:

1. Move the code to `wolts/projects/{name}/`
2. Replace `app.json` with `woltspace.json`:
   ```json
   {
     "name": "{name}",
     "description": "...",
     "stack": "node",
     "install": "npm install",
     "start": "node server.js",
     "keeper": "{your-wolt-name}"
   }
   ```
3. Update base paths from `/app/{name}/` to `/project/{name}/`
4. Test with `push-view /project/{name}/`

See `/projects` and `/new-project` skills for full details.
