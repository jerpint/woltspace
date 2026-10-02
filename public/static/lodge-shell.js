// One-view lodge layout (/shell). One sidebar (lodge pages, then wolts with their
// online sessions) and a tab bar: the first tab is the lodge page, each opened
// session is a tab after it, in the order opened.
//
// The lodge pages and the session screens are the existing pages, loaded in
// frames. A frame named `lodge-shell-page` or `lodge-shell-session` hides its own
// sidebar or top bar (see base.html and tui.html). A hidden frame keeps its full
// size, so a terminal in a background tab never resizes its session.
//
// Backend data is data: DOM nodes and textContent only.
// The rules that decide what a reload may bring back. Pure, so they are tested
// without a browser (test/lodge-shell.test.mjs).
var LodgeShellLogic = (function () {
  'use strict';
  var VIEWS = ['wolts', 'apps', 'sessions'];
  var PLAIN = ['/wolves', '/connectors', '/settings'];
  var NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;

  // The lodge page an address names, in its canonical form, or null. Only pages
  // the sidebar can show: never a session screen, never the layout itself,
  // never another origin. The address is normalized before it is judged.
  function lodgePage(address, origin) {
    if (typeof address !== 'string' || !address) return null;
    var url;
    try { url = new URL(address, origin); } catch (e) { return null; }
    if (url.origin !== origin) return null;
    var path = url.pathname, view = url.searchParams.get('view');
    if (path === '/') {
      if (!view) return '/';
      return VIEWS.indexOf(view) >= 0 ? '/?view=' + view : null;
    }
    if (PLAIN.indexOf(path) >= 0) return path;
    var m = path.match(/^\/w\/([^/]+)\/?$/);
    if (!m) return null;
    var name;
    try { name = decodeURIComponent(m[1]); } catch (e) { return null; }
    return NAME.test(name) ? '/w/' + name : null;
  }

  // The tabs a reload brings back: saved names that are online now, in saved order.
  function restorableTabs(saved, onlineNames) {
    var tabs = [];
    if (saved && Array.isArray(saved.tabs)) saved.tabs.forEach(function (n) {
      if (typeof n === 'string' && NAME.test(n) && onlineNames.indexOf(n) >= 0 && tabs.indexOf(n) < 0) tabs.push(n);
    });
    var current = saved && typeof saved.current === 'string' && tabs.indexOf(saved.current) >= 0 ? saved.current : null;
    return { tabs: tabs, current: current };
  }

  // After a session hands off to another one, its tab becomes the new session's tab.
  function redirectTabs(tabs, current, from, to) {
    var next = tabs.filter(function (n) { return n !== from || tabs.indexOf(to) < 0; })
      .map(function (n) { return n === from ? to : n; });
    return { tabs: next, current: current === from ? to : current };
  }

  return { lodgePage: lodgePage, restorableTabs: restorableTabs, redirectTabs: redirectTabs, NAME: NAME };
})();

(function () {
  'use strict';
  if (typeof document === 'undefined') return;
  // The layout is a top-level page only. Framed (by another page, or by itself)
  // it would restore terminals at whatever size the outer frame gives it.
  if (window.top !== window) { document.body.replaceChildren(); return; }

  var STORE = 'woltspace.shell';
  var POLL_MS = 5000;
  var NAV = [
    { id: 'home', icon: '🏠', label: 'Home', url: '/' },
    { id: 'wolts', icon: '🦫', label: 'Wolts', url: '/?view=wolts' },
    { id: 'apps', icon: '🪓', label: 'Apps', url: '/?view=apps' },
    { id: 'wolves', icon: '🐺', label: 'Wolves', url: '/wolves' },
    { id: 'connectors', icon: '🔌', label: 'Connectors', url: '/connectors' },
  ];
  var SETTINGS = { id: 'settings', icon: '⚙', label: 'Settings', url: '/settings' };
  var SESSION_NAME = LodgeShellLogic.NAME;

  var wolts = {};          // name -> wolt record
  var online = [];         // online sessions, newest activity first
  var known = null;        // names seen so far; null until the first load
  var mine = {};           // sessions started from this page: never announced as new
  var fresh = {};          // name -> true while a session someone else started is unopened
  var folded = {};         // wolt name -> true when folded
  var restingOpen = false;
  var tabs = [];           // session names, in the order they were opened
  var current = null;      // session name shown, or null when the lodge tab is shown
  var page = NAV[0];       // what the lodge tab shows
  var cursor = null;       // tree cursor: 'n:<page>' 'w:<wolt>' 's:<session>' 'r:' 'o:<wolt>' 'f:settings'
  var frames = {};         // session name -> iframe
  var status = {};         // session name -> { state, text }
  var toastSession = null;
  var missing = {};        // tab name -> polls in a row the session was not online
  var starting = {};       // wolt name -> true while a start request is in flight
  var pendingCreate = false;
  var flash = {};

  var $ = function (id) { return document.getElementById(id); };
  var phone = window.matchMedia('(max-width:720px)');
  var lodgeFrame = $('lodge-frame');

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }
  function isOnline(s) { return s.status === 'running' && s.alive === true; }
  function byName(name) { return online.find(function (s) { return s.name === name; }); }
  function woltOf(name) {
    var s = byName(name);
    return s ? s.wolt : name.split('-').slice(0, -3).join('-') || name;
  }
  // A session's title, or the two words of its name when it has none.
  function sessionLabel(name) {
    var s = byName(name);
    return (s && s.title) || name.split('-').slice(-3, -1).join('-') || name;
  }
  function woltLabel(name) {
    var w = wolts[name];
    return (w && (w.display_name || w.name || w.dir)) || name;
  }
  function sprite(woltName, size) {
    var box = el('span', 'ico');
    var type = (wolts[woltName] || {}).type;
    var svg = typeof woltSpriteElement === 'function' ? woltSpriteElement(type, size) : null;
    if (svg) box.appendChild(svg);
    else box.textContent = (typeof WOLT_EMOJI !== 'undefined' && WOLT_EMOJI[type]) || '🦫';
    return box;
  }
  function canStart(woltName) {
    var w = wolts[woltName];
    return !!w && typeof RODENT_TYPES !== 'undefined' && RODENT_TYPES.has(w.type);
  }
  function onlineGroups() {
    var groups = {};
    online.forEach(function (s) { (groups[s.wolt] = groups[s.wolt] || []).push(s); });
    return Object.keys(groups).sort().map(function (w) { return { wolt: w, items: groups[w] }; });
  }
  function restingWolts() {
    var busy = {};
    online.forEach(function (s) { busy[s.wolt] = true; });
    return Object.keys(wolts).filter(function (n) {
      return !busy[n] && (typeof WOLT_TYPES === 'undefined' || WOLT_TYPES.has(wolts[n].type));
    }).sort();
  }
  function visibleKeys() {
    var keys = NAV.map(function (n) { return 'n:' + n.id; });
    onlineGroups().forEach(function (g) {
      keys.push('w:' + g.wolt);
      if (!folded[g.wolt]) g.items.forEach(function (s) { keys.push('s:' + s.name); });
    });
    var rest = restingWolts();
    if (rest.length) {
      keys.push('r:');
      if (restingOpen) rest.forEach(function (n) { keys.push('o:' + n); });
    }
    keys.push('f:settings');
    return keys;
  }

  // ── Rendering ──
  function navRow(n, key) {
    var row = el('div', 'row nav' + (current === null && page.id === n.id ? ' page' : ''));
    row.dataset.key = key;
    row.setAttribute('role', 'treeitem');
    row.appendChild(el('span', 'ico', n.icon));
    row.appendChild(el('span', 'lbl', n.label));
    return row;
  }
  function plusButton(woltName) {
    var plus = el('button', 'plus', '+');
    plus.type = 'button';
    plus.dataset.plus = woltName;
    plus.title = 'New session with ' + woltLabel(woltName);
    plus.setAttribute('aria-label', plus.title);
    plus.disabled = starting[woltName] === true;
    return plus;
  }
  function renderTree() {
    var tree = $('tree');
    tree.replaceChildren();
    NAV.forEach(function (n) { tree.appendChild(navRow(n, 'n:' + n.id)); });

    var sect = el('div', 'sect');
    sect.appendChild(el('span', null, 'Team'));
    var create = el('button', null, '+');
    create.type = 'button';
    create.dataset.create = '1';
    create.title = 'Create wolt';
    create.setAttribute('aria-label', 'Create wolt');
    sect.appendChild(create);
    tree.appendChild(sect);

    var groups = onlineGroups();
    var rest = restingWolts();
    groups.forEach(function (g, gi) {
      var lastWolt = gi === groups.length - 1 && !rest.length;
      var wrow = el('div', 'row t' + (current === null && page.id === 'w:' + g.wolt ? ' page' : ''));
      wrow.dataset.key = 'w:' + g.wolt;
      wrow.setAttribute('role', 'treeitem');
      wrow.setAttribute('aria-expanded', folded[g.wolt] ? 'false' : 'true');
      wrow.appendChild(el('span', 'g ' + (lastWolt ? 'ell' : 'tee')));
      var caret = el('span', 'caret', folded[g.wolt] ? '▸' : '▾');
      caret.dataset.fold = g.wolt;
      wrow.appendChild(caret);
      wrow.appendChild(sprite(g.wolt, 16));
      wrow.appendChild(el('span', 'lbl', woltLabel(g.wolt)));
      if (folded[g.wolt]) wrow.appendChild(el('span', 'wcount', String(g.items.length)));
      if (canStart(g.wolt)) wrow.appendChild(plusButton(g.wolt));
      tree.appendChild(wrow);
      if (folded[g.wolt]) return;
      g.items.forEach(function (s, si) {
        var row = el('div', 'row t');
        row.dataset.key = 's:' + s.name;
        row.setAttribute('role', 'treeitem');
        row.title = s.name;
        if (tabs.indexOf(s.name) >= 0) row.classList.add('open');
        if (s.name === current) row.classList.add('current');
        if (flash[s.name]) { row.classList.add('flash'); delete flash[s.name]; }
        row.appendChild(el('span', 'g ' + (lastWolt ? 'blank' : 'pipe')));
        row.appendChild(el('span', 'g ' + (si === g.items.length - 1 ? 'ell' : 'tee')));
        row.appendChild(el('span', 'dot'));
        row.appendChild(el('span', 'stitle' + (s.title ? '' : ' untitled'), sessionLabel(s.name)));
        if (fresh[s.name]) row.appendChild(el('span', 'newtag', 'new'));
        tree.appendChild(row);
      });
    });

    if (rest.length) {
      var rrow = el('div', 'row t resting');
      rrow.dataset.key = 'r:';
      rrow.setAttribute('role', 'treeitem');
      rrow.setAttribute('aria-expanded', restingOpen ? 'true' : 'false');
      rrow.appendChild(el('span', 'g ell'));
      rrow.appendChild(el('span', 'caret', restingOpen ? '▾' : '▸'));
      rrow.appendChild(el('span', 'lbl', rest.length + ' resting'));
      tree.appendChild(rrow);
      if (restingOpen) rest.forEach(function (n, ni) {
        var row = el('div', 'row t off' + (current === null && page.id === 'w:' + n ? ' page' : ''));
        row.dataset.key = 'o:' + n;
        row.setAttribute('role', 'treeitem');
        row.appendChild(el('span', 'g blank'));
        row.appendChild(el('span', 'g ' + (ni === rest.length - 1 ? 'ell' : 'tee')));
        row.appendChild(sprite(n, 16));
        row.appendChild(el('span', 'lbl', woltLabel(n)));
        if (canStart(n)) row.appendChild(plusButton(n));
        tree.appendChild(row);
      });
    }

    var foot = $('foot');
    foot.replaceChildren();
    foot.appendChild(navRow(SETTINGS, 'f:settings'));
    var keysBtn = el('button', 's-keys-btn', '?');
    keysBtn.type = 'button';
    keysBtn.dataset.keys = '1';
    keysBtn.title = 'Keyboard shortcuts';
    keysBtn.setAttribute('aria-label', 'Keyboard shortcuts');
    keysBtn.setAttribute('aria-expanded', $('keys').classList.contains('show') ? 'true' : 'false');
    foot.appendChild(keysBtn);

    var cur = cursor && $('side-inner').querySelector('[data-key="' + CSS.escape(cursor) + '"]');
    if (cur) { cur.classList.add('cursor'); cur.scrollIntoView({ block: 'nearest' }); }
    $('rail').textContent = phone.matches ? '☰' : (document.body.classList.contains('side-hidden') ? '›' : '‹');
  }

  function renderTabs() {
    var bar = $('tabs');
    bar.replaceChildren();
    var lodge = el('button', 'tab lodge' + (current === null ? ' active' : ''));
    lodge.type = 'button';
    lodge.dataset.lodge = '1';
    lodge.setAttribute('role', 'tab');
    lodge.title = 'The lodge: ' + page.label;
    lodge.appendChild(page.wolt ? sprite(page.wolt, 16) : el('span', 'ico', page.icon));
    lodge.appendChild(el('span', 't', page.label));
    bar.appendChild(lodge);
    tabs.forEach(function (name, i) {
      var wolt = woltOf(name);
      var tab = el('button', 'tab' + (name === current ? ' active' : '') + (byName(name) ? '' : ' offline'));
      tab.type = 'button';
      tab.dataset.name = name;
      tab.setAttribute('role', 'tab');
      tab.title = woltLabel(wolt) + ' · ' + sessionLabel(name);
      if (i < 9) tab.appendChild(el('span', 'n', String(i + 1)));
      tab.appendChild(sprite(wolt, 16));
      tab.appendChild(el('span', 't', sessionLabel(name)));
      var x = el('span', 'x', '×');
      x.dataset.close = name;
      x.setAttribute('aria-label', 'Close tab');
      tab.appendChild(x);
      bar.appendChild(tab);
    });
    var active = bar.querySelector('.tab.active');
    if (active) active.scrollIntoView({ inline: 'nearest', block: 'nearest' });
    document.body.classList.toggle('on-lodge', current === null);
    document.title = (current === null ? page.label : sessionLabel(current)) + ' · woltspace';
  }

  function renderStatus() {
    var st = (current && status[current]) || { state: 'connecting', text: 'connecting' };
    $('status').className = 'tb-status ' + st.state;
    $('status-text').textContent = st.text;
    var doc = current && frameDocument(frames[current]);
    var term = doc && doc.getElementById('btn-term'), view = doc && doc.getElementById('btn-preview');
    $('btn-term').textContent = (term && term.textContent) || 'terminal';
    $('btn-view').textContent = (view && view.textContent) || 'view';
  }

  function showFrames() {
    lodgeFrame.classList.toggle('active', current === null);
    Object.keys(frames).forEach(function (name) { frames[name].classList.toggle('active', name === current); });
    $('offline').classList.toggle('show', current !== null && !frames[current]);
  }

  function save() {
    try {
      localStorage.setItem(STORE, JSON.stringify({
        tabs: tabs, current: current, page: page.url, folded: folded,
        hidden: document.body.classList.contains('side-hidden'),
      }));
    } catch (e) { /* storage may be unavailable; the layout still works */ }
  }
  function render() { renderTree(); renderTabs(); renderStatus(); showFrames(); save(); }

  // ── Frames ──
  function frameDocument(frame) {
    try { return frame && frame.contentDocument; } catch (e) { return null; }
  }
  function focusPane() {
    var frame = current === null ? lodgeFrame : frames[current];
    if (!frame) return;
    try {
      frame.contentWindow.focus();
      var input = frame.contentDocument.querySelector('.xterm-helper-textarea');
      if (input) input.focus();
    } catch (e) { /* frame not ready */ }
  }
  // wake: the person asked for this session (tree, notice, "start it again"), so
  // the session screen may wake it if it rests, as it does outside the layout.
  // Without wake (a reload, switching tabs) the screen only attaches to what runs.
  function sessionFrame(name, wake) {
    if (frames[name]) return frames[name];
    var frame = el('iframe');
    frame.name = 'lodge-shell-session';
    frame.title = 'Session ' + name;
    frame.addEventListener('load', function () {
      var doc = frameDocument(frame);
      if (doc) doc.addEventListener('keydown', onKey, true);
      if (name === current) { renderStatus(); if (document.activeElement !== $('tree')) focusPane(); }
    });
    frame.src = '/tui?session=' + encodeURIComponent(name) + (wake ? '' : '&attach=1');
    frames[name] = frame;
    $('stage').appendChild(frame);
    return frame;
  }
  function dropFrame(name) {
    var frame = frames[name];
    if (!frame) return;
    frame.remove();          // closes the page and its terminal connection; the session keeps running
    delete frames[name];
    delete status[name];
  }

  // ── The lodge tab ──
  function pageFromLocation(loc) {
    var path = loc.pathname, view = new URLSearchParams(loc.search).get('view');
    var m = path.match(/^\/w\/([^/]+)/);
    if (m) {
      var n = decodeURIComponent(m[1]);
      return { id: 'w:' + n, label: woltLabel(n), wolt: n, url: path + loc.search };
    }
    if (path === '/settings') return SETTINGS;
    var hit = NAV.find(function (x) { return x.url === path + (view ? '?view=' + view : ''); });
    if (hit) return hit;
    if (path === '/' && view === 'sessions') return { id: 'sessions', icon: '💬', label: 'Sessions', url: '/?view=sessions' };
    return { id: 'other', icon: '🏠', label: 'Lodge', url: path + loc.search };
  }
  function showPage(p) {
    page = p;
    current = null;
    lodgeFrame.src = p.url;
    closeDrawer();
    render();
  }
  function syncPage() {
    var loc;
    try { loc = lodgeFrame.contentWindow.location; if (!loc || loc.href === 'about:blank' || loc.pathname === '/tui') return; }
    catch (e) { return; }
    var p = pageFromLocation(loc);
    if (p.url === page.url && p.label === page.label) return;
    page = p;
    renderTree(); renderTabs(); save();
  }
  lodgeFrame.addEventListener('load', function () {
    var doc = frameDocument(lodgeFrame);
    if (doc) doc.addEventListener('keydown', onKey, true);
    syncPage();
    if (pendingCreate && openCreateDialog()) pendingCreate = false;
  });
  setInterval(syncPage, 1000);   // lodge pages also change their address without a load

  // ── Tabs ──
  function openSession(name, wake) {
    if (typeof name !== 'string' || !SESSION_NAME.test(name)) return;
    if (tabs.indexOf(name) < 0) tabs.push(name);   // new tabs go at the end
    current = name;
    delete fresh[name];
    if (toastSession === name) hideToast();
    cursor = 's:' + name;
    folded[woltOf(name)] = false;
    if (wake && !frames[name] && !byName(name)) missing[name] = -6;   // it may be waking: give it time
    sessionFrame(name, wake === true);
    closeDrawer();
    render();
    if (document.activeElement !== $('tree')) focusPane();
  }
  function showLodge() { current = null; render(); }
  function closeTab(name) {
    var i = tabs.indexOf(name);
    if (i < 0) return;
    tabs.splice(i, 1);
    dropFrame(name);
    if (current === name) current = tabs[Math.min(i, tabs.length - 1)] || null;
    if (current) sessionFrame(current, false);
    render();
    focusPane();
  }
  function stepTab(d) {            // the lodge tab is position 0
    var all = [null].concat(tabs);
    var next = all[(all.indexOf(current) + d + all.length) % all.length];
    if (next === null) showLodge(); else openSession(next, false);
    focusPane();
  }

  function startSession(woltName) {
    if (starting[woltName]) return;
    starting[woltName] = true;
    renderTree();
    fetch('/sessions/new/lodge', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ wolt: woltName }),
    }).then(function (r) { return r.json().then(function (data) { return { ok: r.ok, data: data }; }); })
      .then(function (res) {
        if (!res.ok || !res.data || typeof res.data.name !== 'string') throw new Error('start');
        mine[res.data.name] = true;
        openSession(res.data.name, true);
        load();
      })
      .catch(function () { showToast("Couldn't start a session with " + woltLabel(woltName), null); })
      .then(function () { delete starting[woltName]; renderTree(); });
  }
  function openCreateDialog() {
    try {
      if (typeof lodgeFrame.contentWindow.openCreateWolt !== 'function') return false;
      lodgeFrame.contentWindow.openCreateWolt();
      return true;
    } catch (e) { return false; }
  }
  function createWolt() {
    showLodge();
    if (openCreateDialog()) return;
    pendingCreate = true;        // the page is not ready: open the dialog when it has loaded
    showPage(NAV[1]);
  }

  function activate(key) {
    if (!key) return;
    var kind = key[0], id = key.slice(2);
    if (kind === 'n') showPage(NAV.find(function (n) { return n.id === id; }) || NAV[0]);
    else if (kind === 'f') showPage(SETTINGS);
    else if (kind === 'w' || kind === 'o') {
      folded[id] = false;
      showPage({ id: 'w:' + id, label: woltLabel(id), wolt: id, url: '/w/' + encodeURIComponent(id) });
    }
    else if (kind === 'r') { restingOpen = !restingOpen; renderTree(); }
    else openSession(id, true);
  }
  function moveCursor(d) {
    var keys = visibleKeys();
    var i = keys.indexOf(cursor);
    cursor = keys[Math.max(0, Math.min(keys.length - 1, i < 0 ? 0 : i + d))];
    renderTree();
  }

  // ── Sidebar ──
  function closeDrawer() { document.body.classList.remove('side-open'); }
  function toggleSide() {
    if (phone.matches) { document.body.classList.toggle('side-open'); return; }
    var hidden = document.body.classList.toggle('side-hidden');
    if (!cursor) cursor = visibleKeys()[0];
    renderTree();
    if (hidden) focusPane(); else $('tree').focus();
    save();
  }

  function showToast(text, session) {
    toastSession = session;
    $('toast-text').textContent = text;
    $('toast-open').hidden = !session;
    $('toast').classList.add('show');
    if (!session) setTimeout(function () { if (!toastSession) $('toast').classList.remove('show'); }, 4000);
  }
  function hideToast() { toastSession = null; $('toast').classList.remove('show'); }

  // ── Events ──
  function sideClick(e) {
    if (e.target.closest('[data-keys]')) { $('keys').classList.toggle('show'); renderTree(); return; }
    if (e.target.closest('[data-create]')) { createWolt(); return; }
    var plus = e.target.closest('[data-plus]');
    if (plus) { startSession(plus.dataset.plus); return; }
    var fold = e.target.closest('[data-fold]');
    if (fold) {
      folded[fold.dataset.fold] = !folded[fold.dataset.fold];
      cursor = 'w:' + fold.dataset.fold;
      renderTree(); save();
      return;
    }
    var row = e.target.closest('.row');
    if (!row) return;
    cursor = row.dataset.key;
    activate(cursor);
  }
  $('tree').addEventListener('click', sideClick);
  $('foot').addEventListener('click', sideClick);
  $('tabs').addEventListener('click', function (e) {
    var x = e.target.closest('[data-close]');
    if (x) { closeTab(x.dataset.close); return; }
    var tab = e.target.closest('.tab');
    if (!tab) return;
    if (tab.dataset.lodge) showLodge(); else openSession(tab.dataset.name, false);
    focusPane();
  });
  $('tabs').addEventListener('auxclick', function (e) {
    var tab = e.target.closest('.tab');
    if (tab && tab.dataset.name && e.button === 1) closeTab(tab.dataset.name);
  });
  $('rail').addEventListener('click', toggleSide);
  $('scrim').addEventListener('click', closeDrawer);
  $('toast-open').addEventListener('click', function () { if (toastSession) openSession(toastSession, true); });
  $('toast-close').addEventListener('click', hideToast);
  $('offline-start').addEventListener('click', function () { if (current) openSession(current, true); });
  function toggleFull(side) {
    var frame = current && frames[current];
    try { frame.contentWindow.toggleFull(side); } catch (e) { return; }
    renderStatus();
  }
  $('btn-term').addEventListener('click', function () { toggleFull('left'); });
  $('btn-view').addEventListener('click', function () { toggleFull('right'); });

  // Runs for keys pressed here and inside the frames (capture phase), so the
  // shortcuts work while a terminal has the focus.
  function onKey(e) {
    if (e.altKey && !e.ctrlKey && !e.metaKey && !e.shiftKey) {
      var handled = true;
      if (e.code === 'KeyE') toggleSide();
      else if (e.code === 'BracketRight') stepTab(1);
      else if (e.code === 'BracketLeft') stepTab(-1);
      else if (e.code === 'KeyW') { if (current) closeTab(current); }
      else if (/^Digit[1-9]$/.test(e.code)) { var t = tabs[Number(e.code.slice(5)) - 1]; if (t) { openSession(t, false); focusPane(); } }
      else handled = false;
      if (handled) { e.preventDefault(); e.stopPropagation(); }
      return;
    }
    if (document.activeElement !== $('tree') || e.target !== $('tree') || e.ctrlKey || e.metaKey || e.altKey) return;
    var k = e.key, used = true, kind = cursor ? cursor[0] : '', id = cursor ? cursor.slice(2) : '';
    if (k === 'j' || k === 'ArrowDown') moveCursor(1);
    else if (k === 'k' || k === 'ArrowUp') moveCursor(-1);
    else if (k === 'Enter' || k === 'o') { activate(cursor); if (kind !== 'r') focusPane(); }
    else if (k === 'l' || k === 'ArrowRight') {
      if (kind === 'w') folded[id] = false;
      else if (kind === 'r') restingOpen = true;
      renderTree(); save();
    }
    else if (k === 'h' || k === 'ArrowLeft') {
      if (kind === 'w') folded[id] = true;
      else if (kind === 'r') restingOpen = false;
      else if (kind === 's') cursor = 'w:' + woltOf(id);
      else if (kind === 'o') cursor = 'r:';
      renderTree(); save();
    }
    else if (k === 'Escape') focusPane();
    else used = false;
    if (used) e.preventDefault();
  }
  document.addEventListener('keydown', onKey);

  // Messages from our own frames: a lodge page asked to open a session, or a
  // session screen reported its connection state.
  window.addEventListener('message', function (e) {
    if (e.origin !== location.origin || !e.data || typeof e.data !== 'object') return;
    if (e.data.type === 'woltspace-shell-open-session' && e.source === lodgeFrame.contentWindow) {
      if (typeof e.data.session === 'string') { mine[e.data.session] = true; openSession(e.data.session, true); load(); }
      return;
    }
    var name = Object.keys(frames).find(function (n) { return frames[n].contentWindow === e.source; });
    if (!name) return;
    if (e.data.type === 'woltspace-shell-redirect') {
      // The session handed off to another one: the tab follows it, and nothing wakes.
      var to = e.data.to;
      if (typeof to !== 'string' || !SESSION_NAME.test(to) || to === name) return;
      var moved = LodgeShellLogic.redirectTabs(tabs, current, name, to);
      dropFrame(name);
      tabs = moved.tabs;
      current = moved.current;
      if (current === to) sessionFrame(to, false);
      render();
      return;
    }
    if (e.data.type === 'woltspace-shell-status') {
      if (e.data.offline === true) {       // an attach-only screen found nothing running
        dropFrame(name);
        render();
        return;
      }
      status[name] = {
        state: ['connected', 'connecting', 'disconnected'].indexOf(e.data.state) >= 0 ? e.data.state : 'connecting',
        text: String(e.data.text || '').slice(0, 24),
      };
      if (name === current) renderStatus();
    }
  });

  // ── Data: the lists the lodge already serves ──
  function load() {
    return Promise.all([
      fetch('/wolts').then(function (r) { return r.json(); }),
      fetch('/sessions?view=lodge').then(function (r) { return r.json(); }),
    ]).then(function (res) {
      var nextWolts = {};
      (Array.isArray(res[0]) ? res[0] : []).forEach(function (w) { nextWolts[w.name || w.dir] = w; });
      var list = ((res[1] && res[1].sessions) || []).filter(isOnline);
      list.forEach(function (s) { if (!nextWolts[s.wolt]) nextWolts[s.wolt] = { name: s.wolt }; });
      list.sort(function (a, b) { return (b.last_activity || 0) - (a.last_activity || 0); });
      var listed = ((res[1] && res[1].sessions) || []);
      wolts = nextWolts;
      online = list;

      // A tab whose session went offline lets go of its terminal, so nothing keeps
      // reconnecting to it. The tab stays; starting it again is a click.
      tabs.forEach(function (name) {
        if (byName(name) || !known || !known[name]) { delete missing[name]; return; }
        missing[name] = (missing[name] || 0) + 1;
        if (missing[name] >= 2) dropFrame(name);
      });

      var first = known === null;
      if (first) known = {};
      var announce = null;
      listed.forEach(function (s) {
        if (!s || typeof s.name !== 'string' || known[s.name]) return;
        known[s.name] = true;
        if (first || !isOnline(s) || mine[s.name] || tabs.indexOf(s.name) >= 0) return;
        fresh[s.name] = true;          // someone else started it: mark it and say so once
        flash[s.name] = true;
        folded[s.wolt] = false;
        announce = s;
      });
      if (first) restore();
      render();
      if (announce) showToast(woltLabel(announce.wolt) + ' started “' + sessionLabel(announce.name) + '”', announce.name);
    }).catch(function () { /* keep what is on screen; the next poll tries again */ });
  }

  // Tabs come back only for sessions that are still online: opening a session
  // screen wakes a resting session, and a reload must never do that by itself.
  function restore() {
    var saved = null;
    try { saved = JSON.parse(localStorage.getItem(STORE)); } catch (e) { saved = null; }
    if (saved && typeof saved === 'object') {
      var back = LodgeShellLogic.restorableTabs(saved, online.map(function (s) { return s.name; }));
      tabs = back.tabs;
      current = back.current;
      if (saved.folded && typeof saved.folded === 'object') {
        Object.keys(saved.folded).forEach(function (k) { if (saved.folded[k] === true) folded[k] = true; });
      }
      var address = LodgeShellLogic.lodgePage(saved.page, location.origin);
      if (address) page = pageFromLocation(new URL(address, location.origin));
      if (saved.hidden === true && !phone.matches) document.body.classList.add('side-hidden');
    }
    lodgeFrame.src = page.url;
    if (current) sessionFrame(current, false);   // attach only; other tabs load when first shown
  }

  load();
  setInterval(function () { if (!document.hidden) load(); }, POLL_MS);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) load(); });
  phone.addEventListener('change', renderTree);
})();
