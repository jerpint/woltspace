---
name: site
description: Your site and the lodge's site shell - the nav drawer, the built-in About / Memory / Settings pages, and how to skin it with site.json or take the site over entirely. Use when building, organizing, restyling or redesigning your site, or when a page count starts to grow.
---

# Your site and the site shell

Your site is plain files in `wolt/site/`, served at `/wolt/<name>/site/` with live reload. The lodge adds one light frame to every page: the **site shell**.

- A small **pill** in the bottom-left corner opens a **drawer**.
- The drawer shows who you are, links to your **built-in pages**, and a **page tree** of your site.
- The tree builds itself from your files. You never maintain a nav.

**Your site is private.** It sits behind the same lodge protection as everything else, for your human only, and it is never shared. Never tell anyone a site page is public or shareable. Everything in the lodge is private by default, apps included; something meant for someone else is shared deliberately through the lodge's sharing mechanism, not by putting it on your site.

The shell is not in your folder. The lodge injects it when it serves a page, next to live reload, and it lives in a shadow root: your CSS cannot break it and it cannot restyle your page.

## Built-in pages

Every wolt gets these, rendered by the lodge from your own files:

| Page | URL | Shows |
|---|---|---|
| About | `/wolt/<name>/_/` | your card (name, creature, engine, model, role) + `identity.md` |
| Memory | `/wolt/<name>/_/memory` | your boot files, windowed the way a session reads them |
| Settings | `/wolt/<name>/_/settings` | wolt.json basics and your site.json look (read-only) |

A site with no `index.html` lands on About, so a brand new wolt has a real home page from its first minute. Keep `identity.md` good: it is your front door.

## Keep the tree readable

- Page titles come from each page's `<title>`. Give every page a short, human title.
- Folders become sections. Group related pages (`plans/`, `notes/`) instead of piling everything at the root.
- Names starting with `_` or `.` are hidden from the tree. Use `_drafts/` for work in progress.
- `index.html` always sorts first.

## Make it yours: four levels

Pick the smallest level that gets you the look you want.

**1. Tokens** - `wolt/site/site.json`:

```json
{
  "title": "uxwolt",
  "tokens": {
    "accent": "#C4531E", "bg": "#F6F2EA", "ink": "#2a2622", "muted": "#6b645b", "line": "#d9d2c4",
    "display_font": "\"Preahvihear\", Georgia, serif", "body_font": "\"DM Sans\", system-ui, sans-serif",
    "emoji": "🦝"
  },
  "fonts_href": "https://fonts.googleapis.com/css2?family=Preahvihear&family=DM+Sans&display=swap"
}
```

Every field is optional. The same tokens skin the built-in pages. By default the shell uses the lodge fonts when your page already loads them and system fonts otherwise; `fonts_href` opts into web fonts.

**2. Your own shell CSS** - `"custom_css": "shell.css"` loads `wolt/site/shell.css` last, inside the shell. Classes you can target: `.pill`, `.drawer`, `.who`, `.avatar`, `.title`, `.role`, `.section`, `.tree`, `.builtins`, `a.active`, `.slot`.

**3. Slots** - `"header_html": "header.html"` and `"footer_html": "footer.html"` drop your HTML into the top and bottom of the drawer: a banner, links, a mascot. Slot files are not listed as pages.

**4. Take the site over** - `"shell": false` in site.json turns the shell off for your whole site. For one page only, add to its `<head>`:

```html
<meta name="wolt-shell" content="off">
```

To put it back, delete that line. Nothing was ever copied into your files, so there is nothing to restore. The built-in pages stay reachable by URL either way.

Livereload covers all of this: edit `site.json` or `shell.css` and the page reloads.

## When a site is not enough

A site is private static files. If what the human wants needs a server, a database, dependencies or a build step, suggest an **app** instead (see the `apps` skill) and ask before creating one. Do not stretch the site into an app.

## Don'ts

- Don't copy the shell's files into your site or rebuild a nav by hand. The tree is generated.
- Don't hide your pages from the tree just to tidy it. Group them in folders.
