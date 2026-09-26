"""
The wolt site shell - one light frame the lodge gives every wolt site.

The lodge owns how it works (a nav drawer whose page tree builds itself from
the files in wolt/site/, plus built-in About / Memory / Settings pages); the
wolt owns how it looks, through wolt/site/site.json. Nothing is ever copied
into a wolt's folder: the shell is two static files in the platform
(public/static/wolt-shell/) and one injected <script> line, next to the
livereload script serve_wolt_site already adds.

site.json (all optional):

    {
      "shell": true,                 # false = the wolt owns the whole site
      "title": "uxwolt",
      "tokens": {"accent": "#C4531E", "bg": "#F6F2EA", "ink": "#2a2622",
                 "muted": "#6b645b", "line": "#d9d2c4",
                 "display_font": "...", "body_font": "...", "emoji": "🦝"},
      "fonts_href": "https://fonts.googleapis.com/css2?...",   # opt-in web fonts
      "custom_css": "shell.css",     # loaded last inside the shell
      "header_html": "header.html",  # slot at the top of the drawer
      "footer_html": "footer.html"   # slot at the bottom of the drawer
    }

A single page opts out with <meta name="wolt-shell" content="off">, and the
lodge-wide kill switch is WOLTSPACE_SITE_SHELL=off.

Sites are private: they sit behind the same lodge protection as everything
else (localhost, or the tunnel's Access gate), which is why the built-in
Memory page may show boot files. Sites are never shared. Apps are private by
default too; anything meant for someone else goes through the lodge's one
deliberate sharing mechanism (sparks and apps), never through a site flag.

Usage:
    from site_shell import inject_shell, shell_manifest, memory_payload
"""

from __future__ import annotations

import html
import json
import os
import re
from pathlib import Path

SHELL_ASSET_BASE = "/static/wolt-shell"

# The fields of wolt.json the shell may show. Anything else (secrets a wolt
# might have put there, lodge internals) never reaches a page.
WOLT_FIELDS = ("name", "type", "role", "description", "harness", "model", "capabilities")

# Boot-file windows: the Memory page shows what a session actually boots with.
CONTEXT_LINES = 80
LEARNINGS_LINES = 40

# A wolt with thousands of files should still get a fast page.
MAX_PAGES = 400
MAX_DEPTH = 4
_SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv"}

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_SHELL_OFF_RE = re.compile(
    r"""<meta[^>]+name\s*=\s*["']wolt-shell["'][^>]+content\s*=\s*["']off["']"""
    r"""|<meta[^>]+content\s*=\s*["']off["'][^>]+name\s*=\s*["']wolt-shell["']""",
    re.I,
)


def load_site_config(sdir: Path) -> dict:
    """Read wolt/site/site.json. A missing or broken file means defaults."""
    try:
        data = json.loads((sdir / "site.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def shell_disabled_for_lodge() -> bool:
    return os.environ.get("WOLTSPACE_SITE_SHELL", "").strip().lower() in ("off", "0", "false", "no")


def shell_wanted(site_cfg: dict, page_html: str) -> bool:
    """Whether this page gets the shell: lodge switch, site.json, then the page."""
    if shell_disabled_for_lodge():
        return False
    if site_cfg.get("shell") is False:
        return False
    return not _SHELL_OFF_RE.search(page_html)


# Titles keyed by (mtime, size): the tree is rebuilt on every page view, but a
# page is only re-read when it changed, so a big site costs one stat per page.
_TITLE_CACHE: dict[str, tuple[int, int, str]] = {}
_TITLE_CACHE_MAX = 5000


def _page_title(path: Path) -> str:
    try:
        st = path.stat()
    except OSError:
        return path.stem
    key = str(path)
    hit = _TITLE_CACHE.get(key)
    if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2]
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            head = f.read(8192)
    except OSError:
        return path.stem
    m = _TITLE_RE.search(head)
    title = " ".join((html.unescape(m.group(1)).strip() if m else "").split()) or path.stem
    if len(_TITLE_CACHE) >= _TITLE_CACHE_MAX:
        _TITLE_CACHE.clear()
    _TITLE_CACHE[key] = (st.st_mtime_ns, st.st_size, title)
    return title


def _slot_files(site_cfg: dict) -> set[str]:
    """Files named as slots or custom css are parts of the shell, not pages."""
    names = set()
    for key in ("header_html", "footer_html", "custom_css"):
        value = site_cfg.get(key)
        if isinstance(value, str) and value:
            names.add(value.strip("/"))
    return names


def page_tree(sdir: Path, site_cfg: dict | None = None) -> list[dict]:
    """The site's html pages as a tree: index first, then pages, then folders.

    Titles come from each page's <title>. Dotfiles, _-prefixed names, build and
    dependency folders, and the shell's own slot files are skipped.
    """
    skip = _slot_files(site_cfg or {})
    count = 0

    def walk(d: Path, rel: str, depth: int) -> list[dict]:
        nonlocal count
        try:
            entries = list(d.iterdir())
        except OSError:
            return []
        entries.sort(key=lambda c: (c.name != "index.html", c.is_dir(), c.name.lower()))
        items: list[dict] = []
        for child in entries:
            if count >= MAX_PAGES:
                break
            name = child.name
            if name.startswith((".", "_")) or name in _SKIP_DIRS:
                continue
            path = f"{rel}{name}"
            if child.is_symlink():
                continue
            if child.is_dir():
                if depth >= MAX_DEPTH:
                    continue
                kids = walk(child, path + "/", depth + 1)
                if kids:
                    items.append({"dir": name, "path": path + "/", "children": kids})
            elif child.suffix.lower() in (".html", ".htm") and path not in skip:
                count += 1
                items.append({"title": _page_title(child), "path": path})
        return items

    return walk(sdir, "", 0) if sdir.is_dir() else []


def wolt_fields(wolt_dir: Path, wolt_name: str) -> dict:
    """The public-safe subset of wolt.json."""
    try:
        cfg = json.loads((wolt_dir / "wolt" / "wolt.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    fields = {k: cfg.get(k) for k in WOLT_FIELDS if cfg.get(k) not in (None, "")}
    fields.setdefault("name", wolt_name)
    fields.setdefault("type", "rodent")
    return fields


def shell_manifest(wolt_name: str, wolt_dir: Path, sdir: Path, site_cfg: dict | None = None) -> dict:
    """Everything the drawer needs, inlined into the page so it draws with no fetch."""
    cfg = load_site_config(sdir) if site_cfg is None else site_cfg
    return {
        "v": 1,
        "wolt": wolt_fields(wolt_dir, wolt_name),
        "site": {k: cfg[k] for k in ("title", "tokens", "fonts_href", "custom_css", "header_html", "footer_html") if k in cfg},
        "tree": page_tree(sdir, cfg),
        "base": f"/wolt/{wolt_name}/site/",
        "builtin": f"/wolt/{wolt_name}/_/",
    }


def _head(path: Path, n: int) -> tuple[str, int]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "", 0
    return "\n".join(lines[:n]), len(lines)


def memory_payload(wolt_dir: Path) -> dict:
    """The boot files, windowed the way a session reads them at start."""
    mem = wolt_dir / "wolt" / "memory"
    identity, _ = _head(mem / "identity.md", 10_000)
    context, context_lines = _head(mem / "context.md", CONTEXT_LINES)
    learnings, learnings_lines = _head(mem / "learnings.md", LEARNINGS_LINES)
    archive_dir = mem / "archive"
    archive = sorted(p.name for p in archive_dir.glob("*.md")) if archive_dir.is_dir() else []
    return {
        "identity": identity,
        "context": context, "context_lines": context_lines, "context_window": CONTEXT_LINES,
        "learnings": learnings, "learnings_lines": learnings_lines, "learnings_window": LEARNINGS_LINES,
        "archive": archive,
    }


def script_json(data) -> str:
    """JSON that is safe inside an inline <script>: no </script>, no line separators."""
    return (
        json.dumps(data, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


def shell_tags(manifest: dict, drawer: bool = True) -> str:
    """The manifest inline (so the drawer draws with no fetch), then shell.js."""
    tags = f"<script>window.__WOLT_SHELL__={script_json(manifest)};</script>"
    if drawer:
        tags += f'<script src="{SHELL_ASSET_BASE}/shell.js"></script>'
    return tags


def inject_shell(page_html: str, manifest: dict) -> str:
    """Add the shell before </body> (or at the end when there is none)."""
    tags = shell_tags(manifest)
    idx = page_html.lower().rfind("</body>")
    if idx == -1:
        return page_html + tags
    return page_html[:idx] + tags + page_html[idx:]
