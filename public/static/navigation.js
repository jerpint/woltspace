// Shared navigation contract for the web lodge and native shells.
// Internal destinations stay in the current client. External destinations
// explicitly leave it, so a native shell can override only that boundary.
(function installWoltspaceNavigation(root) {
  function internal(url, options = {}) {
    if (!url) return;
    if (options.replace) root.location.replace(url);
    else root.location.assign(url);
  }

  function external(url) {
    if (!url) return;
    const opened = root.open(url, '_blank', 'noopener,noreferrer');
    if (opened) opened.opener = null;
  }

  function appDestination(app) {
    if (!app) return '';
    if (app.navigation_url) return app.navigation_url;
    if (app.url) return app.url;
    return app.name ? `/app/${encodeURIComponent(app.name)}/` : '';
  }

  root.WoltspaceNavigation = Object.freeze({
    internal,
    external,
    appDestination,
  });
})(typeof window === 'undefined' ? globalThis : window);
