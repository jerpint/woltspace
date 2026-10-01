/* The wolt site shell: a floating pill + a drawer with the wolt's page tree and
 * its built-in About / Memory / Settings pages.
 *
 * The lodge adds this file to every wolt site page, right after an inline
 * window.__WOLT_SHELL__ manifest (see container/lib/site_shell.py), so the
 * drawer draws on the first frame with no fetch. It lives in a shadow root:
 * it cannot restyle the wolt's page, and the page cannot break it.
 */
(function () {
  if (window.__woltShellMounted) return;
  window.__woltShellMounted = true;
  if (document.querySelector('meta[name="wolt-shell"][content="off"]')) return;

  var ASSETS = '/static/wolt-shell/';
  var EMOJI = { raccoon: '🦝', beaver: '🦫', otter: '🦦', wolf: '🐺', dog: '🐶', rodent: '🐿️' };
  var TABS = [['about', 'About'], ['memory', 'Memory'], ['settings', 'Settings']];

  function el(tag, attrs, kids) {
    var n = document.createElement(tag);
    for (var k in attrs || {}) {
      if (k === 'text') n.textContent = attrs[k];
      else if (k.slice(0, 2) === 'on') n.addEventListener(k.slice(2), attrs[k]);
      else n.setAttribute(k, attrs[k]);
    }
    (kids || []).forEach(function (c) { if (c) n.appendChild(c); });
    return n;
  }
  function norm(p) { return p.replace(/index\.html?$/, ''); }
  // Filenames are data: a '#', '?' or '%' in one must not turn into URL syntax.
  function enc(p) { return String(p).split('/').map(encodeURIComponent).join('/'); }

  function start(m) {
    if (document.body) mount(m);
    else document.addEventListener('DOMContentLoaded', function () { mount(m); });
  }

  if (window.__WOLT_SHELL__) start(window.__WOLT_SHELL__);
  else {
    var hit = /^\/wolt\/([A-Za-z][A-Za-z0-9_-]*)\//.exec(location.pathname);
    if (hit) fetch('/wolt/' + hit[1] + '/_/manifest.json').then(function (r) { return r.json(); }).then(start).catch(function () {});
  }

  function mount(m) {
    var w = m.wolt || {}, site = m.site || {}, t = site.tokens || {};
    var title = site.title || w.display_name || w.name;
    var emoji = t.emoji || EMOJI[w.type] || '🌲';

    // No third-party fonts by default: the stacks below use the lodge fonts
    // when the page already has them and system fonts otherwise. A wolt opts
    // into web fonts with site.json "fonts_href". (@font-face does not apply
    // inside a shadow root, so that link goes on the document.)
    if (site.fonts_href && !document.querySelector('link[data-wolt-shell-fonts]')) {
      document.head.appendChild(el('link', { rel: 'stylesheet', href: site.fonts_href, 'data-wolt-shell-fonts': '' }));
    }

    var host = el('div', { id: 'wolt-shell-host' });
    host.style.visibility = 'hidden';   // until shell.css is in: never flash unstyled
    document.body.appendChild(host);
    var root = host.attachShadow({ mode: 'open' });

    var vars = ':host{all:initial;' +
      '--s-accent:' + (t.accent || '#C4531E') + ';' +
      '--s-bg:' + (t.bg || '#F6F2EA') + ';' +
      '--s-ink:' + (t.ink || '#2a2622') + ';' +
      '--s-muted:' + (t.muted || '#6b645b') + ';' +
      '--s-line:' + (t.line || '#d9d2c4') + ';' +
      '--s-display:' + (t.display_font || '"Preahvihear", ui-serif, Georgia, serif') + ';' +
      '--s-body:' + (t.body_font || '"DM Sans", system-ui, -apple-system, "Segoe UI", sans-serif') + ';}';
    root.appendChild(el('style', { text: vars }));
    var css = el('link', { rel: 'stylesheet', href: ASSETS + 'shell.css' });
    css.addEventListener('load', function () { host.style.visibility = ''; });
    css.addEventListener('error', function () { host.style.visibility = ''; });
    root.appendChild(css);
    if (site.custom_css) root.appendChild(el('link', { rel: 'stylesheet', href: m.base + enc(site.custom_css) }));

    var here = norm(location.pathname);
    var onBuiltin = location.pathname.indexOf(m.builtin) === 0;
    var tabHere = onBuiltin ? (location.pathname.slice(m.builtin.length).split('/')[0] || 'about') : '';

    function link(item) {
      var href = m.base + enc(item.path);
      return el('a', { href: href, class: norm(href) === here ? 'active' : '' }, [el('span', { text: item.title })]);
    }
    function branch(items, depth) {
      var ul = el('ul', { class: 'tree' });
      items.forEach(function (it) {
        if (it.children) {
          var open = here.indexOf(m.base + enc(it.path)) === 0 || (depth === 0 && it.children.length < 6);
          ul.appendChild(el('li', {}, [el('details', open ? { open: '' } : {}, [
            el('summary', { text: it.dir + '/' }), branch(it.children, depth + 1)
          ])]));
        } else ul.appendChild(el('li', {}, [link(it)]));
      });
      return ul;
    }
    function slot(file) {
      if (!file) return null;
      var box = el('div', { class: 'slot' });
      fetch(m.base + enc(file)).then(function (r) { return r.ok ? r.text() : ''; }).then(function (h) { box.innerHTML = h; }).catch(function () {});
      return box;
    }

    var builtins = TABS.map(function (p) {
      return el('a', { href: m.builtin + (p[0] === 'about' ? '' : p[0]), class: 'builtin' + (tabHere === p[0] ? ' active' : '') }, [el('span', { text: p[1] })]);
    });

    var drawer = el('nav', { class: 'drawer', 'aria-label': title + ' site' }, [
      el('div', { class: 'who' }, [
        el('div', { class: 'avatar', text: emoji }),
        el('div', {}, [el('div', { class: 'title', text: title }), el('div', { class: 'role', text: w.role || '' })])
      ]),
      slot(site.header_html),
      el('div', { class: 'section', text: 'Wolt' }),
      el('div', { class: 'builtins' }, builtins),
      el('div', { class: 'section', text: 'Pages' }),
      (m.tree && m.tree.length) ? branch(m.tree, 0) : el('div', { class: 'empty', text: 'No pages yet.' }),
      slot(site.footer_html)
    ]);
    var scrim = el('div', { class: 'scrim', onclick: toggle });
    var pill = el('button', { class: 'pill', type: 'button', 'aria-label': 'Open ' + title + ' menu', onclick: toggle },
      [el('span', { class: 'emoji', text: emoji }), el('span', { class: 'name', text: title })]);

    root.appendChild(scrim);
    root.appendChild(drawer);
    root.appendChild(pill);

    function toggle() { host.toggleAttribute('data-open'); }
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && host.hasAttribute('data-open')) toggle();
    });
  }
})();
