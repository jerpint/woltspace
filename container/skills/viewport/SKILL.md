---
name: viewport
description: Push content to the split view's right pane. Use when you want to show HTML, pages, or artifacts to the user.
---

# Viewport — Split View Control

Every page is a split view: terminal on the left, viewport (iframe) on the right. Each session has its own viewport.

## Push content to the viewport

```bash
# Push a URL to the current session's viewport
curl -s -X POST http://localhost:3000/current?session=main \
  -H 'Content-Type: application/json' \
  -d '{"url": "/some-page.html"}'
```

The URL must be something the server can serve:
- `/index.html` — the wolt's homepage (from `wolt/site/`)
- `/history/{spark-id}` — a saved spark/digest
- Any file in `wolt/site/` — `/about.html`, `/styles.css`, etc.

## Serve new content

To show something new in the viewport:

1. Write an HTML file to `wolt/site/`:
   ```bash
   # Write your HTML to a file
   cat > /workspace/wolt/wolt/site/my-page.html << 'HTML'
   <!DOCTYPE html>
   <html><body><h1>Hello</h1></body></html>
   HTML
   ```

2. Push it to the viewport:
   ```bash
   curl -s -X POST http://localhost:3000/current?session=main \
     -H 'Content-Type: application/json' \
     -d '{"url": "/my-page.html"}'
   ```

The viewport polls every 2 seconds, so the page appears automatically.

## Named sessions

Each tmux session has its own viewport. The session name comes from the `?session=` URL param.

- `/` or `/tui` — default `main` session
- `/tui?session=task-123` — named session with its own viewport

To push to a specific session's viewport:
```bash
curl -s -X POST http://localhost:3000/current?session=task-123 \
  -H 'Content-Type: application/json' \
  -d '{"url": "/my-page.html"}'
```

## Check what's currently displayed

```bash
# Get current viewport URL for a session
curl -s http://localhost:3000/current/meta?session=main
# Returns: {"url": "/index.html", "updated": 1709900000000}
```

## List active sessions

```bash
curl -s http://localhost:3000/sessions
# Returns: [{"name": "main", ...}, {"name": "task-123", ...}]
```

## Tips

- The viewport only shows content served by the server (localhost:3000). External URLs won't work in the iframe due to CORS.
- Files in `wolt/site/` are live-reloaded — edit the HTML and the viewport updates automatically.
- Use `/history/{id}` for sparks/digests that are saved as JSON in `wolt/sparks/`.
