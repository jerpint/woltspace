---
name: woltspace-viewport
description: Push content to the split view's right pane. Use when you want to show HTML, pages, or artifacts to the user.
---

# Viewport — Split View Control

Every page is a split view: terminal on the left, viewport (iframe) on the right. Each session has its own viewport.

## Push content to the viewport

Use `push-view` — it auto-detects your session. That's the only way to update what the user sees.

```bash
push-view /wolt/<name>/site/my-page.html
```

## URL paths

Wolt sites are served at `/wolt/<name>/site/`:
- `wolt/site/hello.html` → `/wolt/<name>/site/hello.html`
- `wolt/site/index.html` → `/wolt/<name>/site/` (index is served at the directory root)

The server runs on **port 7777**. You never need to curl it directly — `push-view` handles everything.

## Live reload

Sites have automatic live reload. When you edit files in `wolt/site/`, the **current page** refreshes automatically — no need to call `push-view` again after the initial push.

**Important:** Livereload only refreshes the page already showing in the viewport. To show a **different** page (e.g. you created a new HTML file), you **must** call `push-view` again with the new path. Livereload won't switch pages for you.

## Tips

- **Use URL paths, not filesystem paths:**
  - ✅ `push-view /wolt/<name>/site/my-page.html`
  - ❌ `push-view /workspace/wolts/<name>/wolt/site/my-page.html`
  - (push-view will auto-translate filesystem paths, but use URL paths for clarity)
- The viewport only shows content served by localhost:7777. External URLs won't work (iframe CORS).
- Each session's viewport is independent. Pushing to one doesn't affect others.
- **Always use `push-view`** — never manually curl to `/current`.
- If you don't know your wolt name, check `$WOLT_NAME` or read `wolt/wolt.json`.
