---
name: viewport
description: Push content to the split view's right pane. Use when you want to show HTML, pages, or artifacts to the user.
---

# Viewport — Split View Control

Every page is a split view: terminal on the left, viewport (iframe) on the right. Each session has its own viewport.

## Push content to the viewport

1. Write an HTML file to `wolt/site/`:
   ```bash
   cat > wolt/site/my-page.html << 'HTML'
   <!DOCTYPE html>
   <html><body><h1>Hello</h1></body></html>
   HTML
   ```

2. Push it:
   ```bash
   push-view /my-page.html
   ```

`push-view` auto-detects which session you're in. No need to pass session names.

## URL paths

Files in `wolt/site/` are served at the root — **no `/site/` prefix**:
- `wolt/site/hello.html` → `/hello.html`
- `wolt/site/index.html` → `/index.html`
- Sparks/digests → `/history/{spark-id}`

## Check what's displayed

```bash
SESSION=$(tmux display-message -p '#S' 2>/dev/null || echo main)
curl -s "http://localhost:3000/current/meta?session=$SESSION"
```

## Showing an app

If you've built a full-stack app (see the `apps` skill), push it to the viewport:

```bash
push-view /app/myapp/
```

Apps served at `/app/{name}/` work in the viewport iframe with no extra setup.

## Tips

- The viewport only shows content served by localhost:3000. External URLs won't work (iframe CORS).
- Files in `wolt/site/` are live-reloaded — edit and the viewport updates automatically.
- Each session's viewport is independent. Pushing to one doesn't affect others.
- **Always use `push-view`** — never manually curl to `/current`.
