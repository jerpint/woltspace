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

