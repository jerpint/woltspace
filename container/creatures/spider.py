"""
🕷️ Spider — Headless Browser

The spider crawls, scrapes, and watches the web. Playwright-backed, quiet and fast.
Used whenever a task requires a real browser (JS-rendered pages, screenshots, form fills).

Role:
  - Headless browser tasks: scrape, screenshot, monitor, interact
  - Invoked per-task (not a long-running service)
  - Returns structured output (HTML, JSON, screenshot) to the caller
  - Caller is typically a beaver session or wolf-dispatched cron

Design:
  - Thin Python wrapper over Playwright async API
  - Single async function: `fetch(url, action=None)` → dict
  - Actions: "html", "screenshot", "pdf", "click+extract", "wait_for"
  - Timeout + error handling baked in — never hangs the caller
  - Runs as a subprocess invoked by run-session.sh or directly by bot tools

Entry point:
  - CLI: `python -m creatures.spider <url> [--action html|screenshot]`
  - Or imported as a library by beaver sessions

TODO (implementation):
  - Install playwright in Dockerfile (chromium only, minimal footprint)
  - Implement fetch() with basic action support
  - Add `screenshot` tool to bot tool registry (triggers spider)
  - Consider sandboxing (spider runs in restricted network env)
  - Cache layer (avoid re-fetching the same URL within a session)
"""

# Placeholder — not yet implemented


async def fetch(url: str, action: str = "html") -> dict:
    raise NotImplementedError("spider is not yet implemented")
