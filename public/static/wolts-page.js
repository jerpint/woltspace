// Wolts page (/?view=wolts): every wolt in the lodge, awake ones first, then by last chat.
// Tap a card for the wolt's page, + for a new chat, the session count for its recent sessions,
// ⋯ for its settings. Reads the same allWolts / allSessions lodge.js already loads.
(() => {
  const view = document.getElementById('wolts-view');
  if (!view) return;
  const expanded = new Set();
  let query = '';

  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; };
  const woltUrl = (name, tab) => `/w/${encodeURIComponent(name)}${tab ? `?tab=${tab}` : ''}`;
  const stamp = s => s.last_activity || s.created_at || 0;
  const when = ts => {
    const d = new Date(ts * 1000), today = new Date().toDateString(), yesterday = new Date(Date.now() - 86400e3).toDateString();
    const t = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    return d.toDateString() === today ? t : d.toDateString() === yesterday ? `Yesterday ${t}` : `${d.toLocaleDateString([], { month: 'short', day: 'numeric' })} ${t}`;
  };
  const sessionState = sessionStateText;

  function closeMenu() { document.querySelector('.wolts-menu')?.remove(); }

  function openMenu(button, name) {
    closeMenu();
    const menu = el('div', 'wolts-menu');
    menu.setAttribute('role', 'menu');
    const item = (label, href) => { const a = el('a', '', label); a.href = href; a.setAttribute('role', 'menuitem'); menu.appendChild(a); };
    const later = label => { const b = el('button', '', label); b.disabled = true; b.appendChild(el('small', '', 'later')); menu.appendChild(b); };
    item('Open page', woltUrl(name));
    item('⚙ Settings', woltUrl(name, 'settings'));
    item('About & memory', woltUrl(name, 'about'));
    menu.appendChild(el('hr'));
    later('Rename'); later('Archive'); later('Delete wolt');
    menu.addEventListener('click', e => e.stopPropagation());
    document.body.appendChild(menu);
    const r = button.getBoundingClientRect();
    menu.style.left = `${Math.max(8, r.right - menu.offsetWidth)}px`;
    menu.style.top = `${r.bottom + menu.offsetHeight + 8 > innerHeight ? r.top - menu.offsetHeight - 4 : r.bottom + 4}px`;
    setTimeout(() => document.addEventListener('click', closeMenu, { once: true }));
    addEventListener('scroll', closeMenu, { once: true, capture: true });
  }

  function card(x) {
    const { w, name, sessions, open, working, last, total } = x;
    const isOpen = expanded.has(name);
    const wrap = el('div', `wolts-card${isOpen ? ' expanded' : ''}`);
    const row = el('div', 'wolts-row');
    row.tabIndex = 0;
    row.onclick = () => { window.location.href = woltUrl(name); };
    row.onkeydown = e => { if (e.target === row && (e.key === 'Enter' || e.key === ' ')) row.click(); };

    const avatar = el('div', 'wolts-avatar');
    const sprite = woltSpriteAvatar(w.type, 36);
    if (sprite) avatar.innerHTML = sprite; else avatar.textContent = WOLT_EMOJI[w.type] || '🦫';

    const body = el('div', 'wolts-body');
    body.appendChild(el('div', 'wolts-name', name));
    const line = el('div', 'wolts-sub', `${w.type} · `);
    line.appendChild(open.length ? el('span', working ? 'wolts-on' : '', woltStateText(x))
      : document.createTextNode(last ? `last chat ${compactAge(Date.now() / 1000 - last)} ago` : 'never chatted'));
    body.appendChild(line);
    const countLine = el('div', 'wolts-sub');
    if (total) {
      const count = el('button', `wolts-count${isOpen ? ' open' : ''}`);
      count.setAttribute('aria-expanded', String(isOpen));
      count.appendChild(document.createTextNode(`${total} session${total === 1 ? '' : 's'} `));
      count.appendChild(el('span', 'wolts-chev', '▾'));
      count.onclick = e => { e.stopPropagation(); expanded.has(name) ? expanded.delete(name) : expanded.add(name); renderWoltsPage(); };
      countLine.appendChild(count);
    } else {
      countLine.textContent = 'no sessions yet';
    }
    body.appendChild(countLine);
    row.append(avatar, body);

    if (RODENT_TYPES.has(w.type)) {
      const add = el('button', 'wolts-add', '+');
      add.title = `New chat with ${name}`; add.setAttribute('aria-label', add.title);
      add.onclick = e => { e.stopPropagation(); startSession(name); };
      row.appendChild(add);
    }
    const more = el('button', 'wolts-more', '⋯');
    more.title = `Manage ${name}`; more.setAttribute('aria-label', more.title); more.setAttribute('aria-haspopup', 'menu');
    more.onclick = e => { e.stopPropagation(); openMenu(more, name); };
    row.appendChild(more);
    wrap.appendChild(row);

    if (isOpen) {
      const list = el('div', 'wolts-sessions');
      sessions.slice(0, 8).forEach(s => {
        const live = sessionIsOpen(s);
        // a session opens if it's live or has a conversation to go back to (opening wakes it)
        const openable = live || s.openable === true;
        const a = el(openable ? 'a' : 'div', `wolts-session${openable ? '' : ' ended'}`);
        if (openable) a.href = `/tui?session=${encodeURIComponent(s.name)}`;
        else a.title = 'No conversation to reopen';
        a.appendChild(el('span', `session-dot ${live ? 'running' : 'stopped'}`));
        a.appendChild(el('span', 'wolts-session-title', (s.title || s.name || 'untitled session').trim()));
        a.appendChild(el('span', 'wolts-session-meta', sessionState(s)));
        list.appendChild(a);
      });
      if (total > sessions.length) {
        const all = el('a', 'wolts-session-all', `All ${total} on ${name}'s page ›`);
        all.href = woltUrl(name);
        list.appendChild(all);
      }
      wrap.appendChild(list);
    }
    return wrap;
  }

  window.renderWoltsPage = function () {
    if (!view.classList.contains('active')) return;
    const all = allWolts.filter(w => WOLT_TYPES.has(w.type)).map(woltSessionSummary)
      .sort((a, b) => ((b.open.length > 0) - (a.open.length > 0)) || (b.last - a.last));
    const q = query.trim().toLowerCase();
    const hits = all.filter(x => !q || x.name.toLowerCase().includes(q) || (x.w.type || '').includes(q));
    const awake = hits.filter(x => x.open.length), resting = hits.filter(x => !x.open.length);
    const focused = document.activeElement?.id === 'wolts-search';

    const header = el('div', 'main-header');
    const titles = el('div');
    titles.append(el('div', 'main-title', 'Wolts'), el('div', 'main-subtitle', 'Everyone in your lodge. Tap one for its page, or start a chat.'));
    const actions = el('div', 'header-actions');
    const create = el('button', 'btn btn-ghost', '+ Create wolt');
    create.onclick = e => openCreateWolt(e);
    actions.appendChild(create);
    header.append(titles, actions);

    const content = el('div', 'wolts-content');
    const search = el('input', 'wolts-search');
    search.id = 'wolts-search'; search.type = 'search'; search.placeholder = 'Find a wolt'; search.autocomplete = 'off'; search.value = query;
    search.oninput = e => { query = e.target.value; renderWoltsPage(); };
    content.appendChild(search);
    const group = (label, items) => {
      if (!items.length) return;
      content.appendChild(el('div', 'wolts-group', `${label} · ${items.length}`));
      const grid = el('div', 'wolts-grid');
      items.forEach(x => grid.appendChild(card(x)));
      content.appendChild(grid);
    };
    group('Awake', awake);
    group('Resting', resting);
    if (!hits.length) content.appendChild(el('div', 'wolts-quiet', all.length ? 'No wolt matches.' : 'No wolts yet.'));

    view.replaceChildren(header, content);
    if (focused) { search.focus(); search.setSelectionRange(query.length, query.length); }
  };

  // showView lives in lodge.js; draw this page whenever it becomes the active view
  const show = window.showView;
  window.showView = function (name) {
    show(name);
    if (name === 'wolts') { renderWoltsPage(); window.scrollTo(0, 0); }
  };
})();
