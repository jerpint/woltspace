# Client surfaces and navigation

Woltspace has one product UI delivered through several client surfaces:

- a regular desktop browser;
- an installed PWA, including mobile;
- the macOS desktop shell; and
- a browser or shell connected to a remote lodge through Cloudflare.

The server-rendered Jinja templates and shared static CSS/JavaScript are the
canonical UI. The macOS app is a native host around that UI, not a separate
frontend. Design-system and product changes should normally be made once in
the shared web layer so every client receives them together.

## Navigation contract

Navigation is classified by destination, not by which client is displaying
the lodge.

### Internal Woltspace destinations

Lodge screens, settings, sessions, and apps stay in the current client. A
normal click must not create a new browser tab or a second desktop window.

- Prefer real anchors for destinations known while rendering. This preserves
  browser features such as copy-link, long-press, and explicit modifier-click.
- Use `WoltspaceNavigation.internal(url)` when the destination is returned by
  an asynchronous action, such as creating a session.
- Keep lodge and session URLs relative (`/settings`, `/tui?...`). They then
  inherit the origin that opened Woltspace: local Docker, a PWA, or Cloudflare.

### App destinations

The browser must not construct app hostnames or ports. The `/apps` API owns
the canonical `url` for an app, currently `/app/{name}/`. That server route
can resolve local ports and configured wildcard tunnel domains without the
client knowing the deployment topology.

Use `WoltspaceNavigation.appDestination(app)` to select a destination. It
prefers a future environment-specific `navigation_url`, then the API's `url`,
and only derives `/app/{name}/` as a compatibility fallback.

### External destinations

Source repositories, documentation on another origin, and authentication
pages may leave the current client. Real external anchors should use
`target="_blank"` with `rel="noopener noreferrer"`. Programmatic external
flows use `WoltspaceNavigation.external(url)`.

A native shell may translate that external boundary into the operating
system's default browser. It must not reinterpret internal links as external.

## Native-shell boundary

The desktop shell may own capabilities that browsers cannot provide cleanly:

- Docker availability, startup, and recovery UI;
- application lifecycle, tray, and menu integration;
- native notifications and deep links;
- opening external URLs in the system browser; and
- an injectable lodge base URL for local and remote modes.

Product screens, navigation structure, forms, configuration UX, responsive
layout, and design-system primitives remain in the shared web application.
Avoid copying those screens into native markup.

## PWA and mobile expectations

The PWA is a first-class client, not merely a fallback. Shared UI changes
should preserve responsive layout, touch targets, long-pressable links,
same-view internal navigation, and recovery through browser history.

## Change checklist

When adding or changing a screen:

1. Build it in shared templates and static assets unless it is strictly an OS
   capability.
2. Keep internal routes relative and in the current view.
3. Ask the server for app destinations; never hardcode `localhost` or ports in
   frontend code.
4. Mark truly external navigation explicitly and safely.
5. Verify the responsive/touch path as well as desktop browser behavior.
6. Extend `test/navigation.test.mjs` when the navigation contract changes.

