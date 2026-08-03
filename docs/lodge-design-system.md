# Lodge design system

The lodge stays server-rendered. Jinja owns page structure and initial state;
small browser modules own interaction. The `ds-*` primitives make new settings
screens consistent without turning the lodge into a single-page application.

## Source map

- `templates/components/ui.html` — Jinja macros for page headers, panels,
  choice cards, fields, toggles, tabs, dialogs, and empty states.
- `public/static/design-system.css` — shared spacing, radius, focus, feedback,
  layout, and component styles. Names are scoped under `ds-*`.
- `public/static/settings.js` — page-local progressive enhancement for the
  engine settings screen.
- `templates/settings.html` — reference implementation using real configuration
  endpoints, not a component showroom.

## Rules of the trail

1. Render useful labels, values, and help text in Jinja. JavaScript enhances a
   complete screen; it does not manufacture the whole page.
2. Prefer a shared macro and a `ds-*` class before adding a page-specific
   primitive. Page-specific layout names are fine when they compose primitives.
3. Keep mutations narrow and explicit. Show saving, saved, and error states,
   revert optimistic controls on failure, and announce feedback with live regions.
4. Every control needs a label and keyboard focus. Do not encode state using
   color alone.
5. Build narrow-screen behavior with the primitive. A configuration page is not
   complete if its form becomes awkward below 620px.

## Adding a settings screen

Extend `base.html`, import only the macros needed from `components/ui.html`, and
wrap the content in `ds-page`. Add the screen to the Settings index when it has a
real route and persistence behavior; avoid dead navigation pretending to be a
feature. Put page behavior in its own module and keep fetch endpoints versionable
and independent from Tauri. The desktop shell consumes the same lodge page.
