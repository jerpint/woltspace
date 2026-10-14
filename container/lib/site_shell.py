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

Everything in the lodge is private by default - sites, apps and sparks - and
sits behind the lodge's own protection (localhost, or the tunnel's Access
gate). That is why the built-in Memory page may show boot files here. Sharing
is a separate, deliberate act through the lodge's one sharing mechanism, never
a site.json flag, and a shared site is served away from the lodge on its own
origin and its own auth: it never carries the Memory or Settings pages.

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
WOLT_FIELDS = ("name", "display_name", "type", "role", "description", "harness", "model", "capabilities")

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


# A slot is a fragment of the drawer, not a page; this is plenty for one.
MAX_SLOT_BYTES = 64 * 1024


def slot_html(sdir: Path, name: object) -> str | None:
    """The header/footer slot file, read from inside the site dir or nothing."""
    if not isinstance(name, str) or not name:
        return None
    path = sdir / name.strip("/")
    try:
        path.resolve().relative_to(sdir.resolve())
        if not path.is_file() or path.stat().st_size > MAX_SLOT_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None


# What a wolt's site.json may change about the lodge-drawn built-in pages:
# the title, a few colours, the avatar emoji. Each value is checked, not
# passed through - a hex colour is a hex colour, never `url(...)`, a font
# stack or anything else CSS would act on. Stylesheets (`custom_css`), font
# imports (`fonts_href`), slots and font tokens stay on the wolt's own site
# pages, which are the wolt's own documents.
_HEX_COLOUR_RE = re.compile(r"#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})")
OWNER_COLOUR_TOKENS = ("accent", "bg", "ink", "muted", "line")
_TITLE_MAX = 60
_EMOJI_MAX = 8


def owner_look(cfg: dict) -> dict:
    """The checked subset of site.json a built-in page may use."""
    look: dict = {}
    title = cfg.get("title")
    if isinstance(title, str) and title.strip():
        look["title"] = " ".join(title.split())[:_TITLE_MAX]
    tokens = cfg.get("tokens") if isinstance(cfg.get("tokens"), dict) else {}
    safe = {k: v.strip() for k, v in tokens.items()
            if k in OWNER_COLOUR_TOKENS and isinstance(v, str) and _HEX_COLOUR_RE.fullmatch(v.strip())}
    emoji = tokens.get("emoji")
    if isinstance(emoji, str) and 0 < len(emoji.strip()) <= _EMOJI_MAX and emoji.isprintable():
        safe["emoji"] = emoji.strip()
    if safe:
        look["tokens"] = safe
    return look


def shell_manifest(wolt_name: str, wolt_dir: Path, sdir: Path, site_cfg: dict | None = None,
                   slots: bool = False, owner_page: bool = False) -> dict:
    """Everything the drawer needs, inlined into the page so it draws with no fetch.

    `slots` inlines the header/footer HTML too. Only a wolt's own site page
    asks for that: it is served sandboxed (opaque origin), so it could not
    fetch the slots itself, and the wolt's markup inside the wolt's own page
    adds nothing. shell.js draws no slot it was not handed.

    `owner_page` is for the lodge-drawn built-in pages, which carry the wolt's
    boot files: the site look is cut down to owner_look(), so no wolt
    stylesheet, font import, slot or free-form token reaches that document.
    One manifest shape per page kind, so the two renderers cannot disagree.
    """
    cfg = load_site_config(sdir) if site_cfg is None else site_cfg
    if owner_page:
        site = owner_look(cfg)
    else:
        site = {k: cfg[k] for k in ("title", "tokens", "fonts_href", "custom_css", "header_html", "footer_html") if k in cfg}
    if slots and not owner_page:
        found = {k: slot_html(sdir, cfg.get(f"{k}_html")) for k in ("header", "footer")}
        found = {k: v for k, v in found.items() if v is not None}
        if found:
            site["slots"] = found
    return {
        "v": 1,
        "wolt": wolt_fields(wolt_dir, wolt_name),
        "site": site,
        "tree": page_tree(sdir, cfg),
        "base": f"/wolt/{wolt_name}/site/",
        "builtin": f"/wolt/{wolt_name}/_/",
    }


# What a site page served on its own content host (the app gateway) may know
# about its wolt. Nothing that describes how the wolt runs.
PUBLIC_WOLT_FIELDS = ("name", "display_name", "type", "role", "description")
SITE_LOOK_FIELDS = ("title", "tokens", "fonts_href", "custom_css", "header_html", "footer_html")
# The shell assets a content host serves itself, under its reserved namespace.
PUBLIC_ASSET_BASE = "/_woltspace/assets/"
PUBLIC_ASSETS = ("shell.js", "shell.css")


def public_shell_manifest(
    wolt_name: str, wolt_dir: Path, sdir: Path, site_cfg: dict | None = None,
    *, lodge_origin: str | None = None,
) -> dict:
    """The manifest for a page on the site's own origin: content, no lodge.

    Built apart from `shell_manifest` on purpose: no memory, no engine
    settings, no lodge paths. Pages and assets are addressed from the site
    root. The built-in About / Memory / Settings pages stay on the lodge, so
    `builtin` is an absolute lodge address and only present when the caller
    says the viewer is the owner; a shared viewer gets content navigation only.
    """
    cfg = load_site_config(sdir) if site_cfg is None else site_cfg
    fields = wolt_fields(wolt_dir, wolt_name)
    manifest = {
        "v": 1,
        "wolt": {k: fields[k] for k in PUBLIC_WOLT_FIELDS if k in fields},
        "site": {k: cfg[k] for k in SITE_LOOK_FIELDS if k in cfg},
        "tree": page_tree(sdir, cfg),
        "base": "/",
        "assets": PUBLIC_ASSET_BASE,
    }
    # The wolt's own header/footer HTML, on its own origin: inlined because
    # shell.js draws only slots it was handed (it never fetches them).
    found = {k: slot_html(sdir, cfg.get(f"{k}_html")) for k in ("header", "footer")}
    found = {k: v for k, v in found.items() if v is not None}
    if found:
        manifest["site"]["slots"] = found
    if lodge_origin:
        manifest["builtin"] = f"{lodge_origin.rstrip('/')}/wolt/{wolt_name}/_/"
    return manifest


def _head(path: Path, n: int) -> tuple[str, int]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "", 0
    return "\n".join(lines[:n]), len(lines)


def memory_for_tab(wolt_dir: Path, tab: str) -> dict:
    """Only what one built-in tab shows: About gets identity, Memory gets the
    boot files, Settings gets nothing. Inlined into the page, so each page
    carries no more of the wolt's memory than it draws."""
    if tab == "memory":
        return memory_payload(wolt_dir)
    if tab == "about":
        identity, _ = _head(wolt_dir / "wolt" / "memory" / "identity.md", 10_000)
        return {"identity": identity}
    return {}


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


def _shell_asset_version() -> str:
    """Changes whenever the installed shell.js does, so browsers and edge
    caches never keep serving the previous release's drawer."""
    try:
        shell = Path(__file__).resolve().parents[2] / "public" / "static" / "wolt-shell" / "shell.js"
        stat = shell.stat()
        return f"{int(stat.st_mtime)}-{stat.st_size}"
    except OSError:
        return "0"


def shell_tags(manifest: dict, drawer: bool = True) -> str:
    """The manifest inline (so the drawer draws with no fetch), then shell.js.

    The script comes from wherever the manifest says the shell assets live:
    the lodge's static path by default, a content host's reserved namespace
    for a page served on the site's own origin.
    """
    tags = f"<script>window.__WOLT_SHELL__={script_json(manifest)};</script>"
    if drawer:
        base = manifest.get("assets") or f"{SHELL_ASSET_BASE}/"
        tags += f'<script src="{base}shell.js?v={_shell_asset_version()}"></script>'
    return tags


def inject_shell(page_html: str, manifest: dict) -> str:
    """Add the shell before </body> (or at the end when there is none)."""
    tags = shell_tags(manifest)
    idx = page_html.lower().rfind("</body>")
    if idx == -1:
        return page_html + tags
    return page_html[:idx] + tags + page_html[idx:]
