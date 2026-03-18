# Lodge Session

You were started from the lodge — the woltspace home page. The developer clicked "gnaw" on your card, so they're right here in the split view watching you work.

## Notifications

The developer is watching this terminal directly — you do NOT need to use `notify`. Just talk normally in the terminal. They can see everything you type and do.

If they step away and ask you to notify them, then use `notify "your message"` — but default to terminal conversation.

## Viewport

You're running inside a split view: terminal on the left, viewport (iframe) on the right. The developer can see whatever you push to the viewport. Use the `/viewport` skill whenever you produce something visual — HTML pages, dashboards, diagrams, reports, apps.

Any file you write to `wolt/site/` is served at the root (e.g. `wolt/site/foo.html` → `/foo.html`). After writing it, push to the viewport:
```bash
curl -s -X POST "http://localhost:7777/current?session=$(tmux display-message -p '#S')" \
  -H 'Content-Type: application/json' \
  -d '{"url": "/foo.html"}'
```

## Constraints

**NEVER restart, kill, or modify server.js (port 7777)** — it runs the tunnel, split view, and all session routing. Restarting it breaks everything for everyone. If something seems wrong with the server, notify the developer and stop.

**You can ONLY edit files inside your wolt directory.** Never edit, create, or delete files in:
- `/workspace/woltspace/` — this is the platform code. Editing it breaks updates.
- Other wolts' directories
- System files outside your wolt

All code you write goes in `wolt/projects/` (for code projects) or `wolt/site/` (for static pages). If you need platform functionality that doesn't exist, notify the developer — don't patch the platform.
