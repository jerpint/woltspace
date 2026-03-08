---
name: viewport
description: Push content to the split view's right pane. Use when you want to show HTML, pages, or artifacts to the user.
---

# Viewport — Split View Control

Every page is a split view: terminal on the left, viewport (iframe) on the right. Each session has its own viewport.

## Know your session

You're always inside a tmux session. Get your session name:
```bash
tmux display-message -p '#S'
```
This returns `main`, `task-45002`, etc. **Always use this** — never hardcode a session name.

## Push content to the viewport

1. Write an HTML file to `wolt/site/`:
   ```bash
   cat > /workspace/wolt/wolt/site/my-page.html << 'HTML'
   <!DOCTYPE html>
   <html><body><h1>Hello</h1></body></html>
   HTML
   ```

2. Push it to your session's viewport:
   ```bash
   curl -s -X POST "http://localhost:3000/current?session=$(tmux display-message -p '#S')" \
     -H 'Content-Type: application/json' \
     -d '{"url": "/my-page.html"}'
   ```

The viewport polls every 2 seconds, so the page appears automatically.

## URL paths

Files in `wolt/site/` are served at the root — **no `/site/` prefix**:
- `wolt/site/hello.html` → `/hello.html`
- `wolt/site/index.html` → `/index.html`
- Sparks/digests → `/history/{spark-id}`

## Check what's displayed

```bash
SESSION=$(tmux display-message -p '#S')
curl -s "http://localhost:3000/current/meta?session=$SESSION"
```

## List active sessions

```bash
curl -s http://localhost:3000/sessions
```

## Tips

- The viewport only shows content served by localhost:3000. External URLs won't work (iframe CORS).
- Files in `wolt/site/` are live-reloaded — edit and the viewport updates automatically.
- Each session's viewport is independent. Pushing to one doesn't affect others.
