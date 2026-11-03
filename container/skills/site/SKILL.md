---
name: site
description: Your site, served by the Mill on its own origin, and the lodge's built-in About / Memory / Settings pages with their nav drawer, skinned with site.json. Use when building, organizing, restyling or redesigning your site, or when a page count starts to grow.
---

# Your site and the site shell

Your site is plain files in `wolt/site/`, served by the Mill at `http://<name>.sites.localhost:7777/`, on an origin of its own, with live reload (`/wolt/<name>/site/` redirects there). Your pages are served exactly as you write them. The **site shell** lives on your built-in pages at `/wolt/<name>/_/`:

- A small **pill** in the bottom-left corner opens a **drawer**.
- The drawer shows who you are, links to your **built-in pages**, and a **page tree** of your site.
- The tree builds itself from your files. You never maintain a nav.

**Your site is private.** It's for your human only, locally and, if they turn it on, through the lodge's tunnel behind Cloudflare Access. There is no sharing a site with other people. Never tell anyone a page is shared.

The shell is not in your folder, and it isn't added to your pages: they're served exactly as you write them. It frames the built-in pages, in a shadow root.

## Built-in pages

Every wolt gets these, rendered by the lodge from your own files:

| Page | URL | Shows |
|---|---|---|
| About | `/wolt/<name>/_/` | your card (name, creature, engine, model, role) + `identity.md` |
| Memory | `/wolt/<name>/_/memory` | your boot files, windowed the way a session reads them |
| Settings | `/wolt/<name>/_/settings` | wolt.json basics and your site.json look (read-only) |

Give your site an `index.html`: a site folder without one has no home page. Your About page at `/wolt/<name>/_/` always exists, from `identity.md`, so keep that good.

## Keep the tree readable

- Page titles come from each page's `<title>`. Give every page a short, human title.
- Folders become sections. Group related pages (`plans/`, `notes/`) instead of piling everything at the root.
- Names starting with `_` or `.` are hidden from the tree. Use `_drafts/` for work in progress.
- `index.html` always sorts first.

## Make the built-in pages yours: four levels

Pick the smallest level that gets you the look you want. These skin the drawer and the built-in pages; your own pages are styled by you.

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

**4. Turn the drawer off** - `"shell": false` in site.json turns it off on the built-in pages. Your own pages never had it: they're yours, as written.

Livereload covers all of this: edit `site.json` or `shell.css` and the page reloads.

## When a site is not enough

A site is private static files. If what the human wants needs a server, a database, dependencies or a build step, suggest an **app** instead (see the `apps` skill) and ask before creating one. Do not stretch the site into an app.

## Don'ts

- Don't copy the shell's files into your site or rebuild a nav by hand. The tree is generated.
- Don't hide your pages from the tree just to tidy it. Group them in folders.
