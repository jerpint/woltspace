// Wolts page (/?view=wolts): every wolt in the lodge, online ones first, then by last chat.
// Tap a card for the wolt's page, + for a new chat, the session count for its recent sessions,
// ⋯ for its settings or to archive it. Reads the same allWolts / allSessions lodge.js already loads;
// archived wolts come from /wolts/archived, listed at the bottom with Unarchive and Delete.
(() => {
  const view = document.getElementById('wolts-view');
  if (!view) return;
  const expanded = new Set();
  let query = '';
  let archived = [], archivedKey = '[]', archivedOpen = false;

  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; };
  const woltUrl = (name, tab) => `/w/${encodeURIComponent(name)}${tab ? `?tab=${tab}` : ''}`;
  const stamp = s => s.last_activity || s.created_at || 0;
  const when = ts => {
    const d = new Date(ts * 1000), today = new Date().toDateString(), yesterday = new Date(Date.now() - 86400e3).toDateString();
    const t = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    return d.toDateString() === today ? t : d.toDateString() === yesterday ? `Yesterday ${t}` : `${d.toLocaleDateString([], { month: 'short', day: 'numeric' })} ${t}`;
  };

  function closeMenu() { document.querySelector('.wolts-menu')?.remove(); }

  // Asked on every render; a different answer draws the page again.
  async function refreshArchived() {
    try {
      const r = await fetch('/wolts/archived');
      const key = r.ok ? JSON.stringify(await r.json()) : '[]';
      if (key !== archivedKey) { archivedKey = key; archived = JSON.parse(key); renderWoltsPage(); }
    } catch {}
  }

  // Archive, unarchive and delete are the lodge's; the page asks, calls and reports a refusal.
  async function archiveAction(method, url, question) {
    if (question && !confirm(question)) return;
    try {
      const r = await fetch(url, { method });
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        alert([body.error || `failed (${r.status})`, ...(body.held || []).map(h => `${h.session}: ${h.reason}`)].join('\n'));
      }
    } catch (e) { alert(e.message); }
    await loadWolts();
    renderWoltsPage();
  }

  function openMenu(button, name, shown) {
    closeMenu();
    const menu = el('div', 'wolts-menu');
    menu.setAttribute('role', 'menu');
    const item = (label, href) => { const a = el('a', '', label); a.href = href; a.setAttribute('role', 'menuitem'); menu.appendChild(a); };
    item('Open page', woltUrl(name));
    item('⚙ Settings', woltUrl(name, 'settings'));
    item('About & memory', woltUrl(name, 'about'));
    menu.appendChild(el('hr'));
    const archive = el('button', '', 'Archive');
    archive.setAttribute('role', 'menuitem');
    archive.onclick = () => { closeMenu(); return archiveAction('POST', `/wolts/${encodeURIComponent(name)}/archive`,
      `Archive ${shown}? Its sessions stop and it leaves the lodge until you unarchive it.`); };
    menu.appendChild(archive);
    menu.addEventListener('click', e => e.stopPropagation());
    document.body.appendChild(menu);
    const r = button.getBoundingClientRect();
    menu.style.left = `${Math.max(8, r.right - menu.offsetWidth)}px`;
    menu.style.top = `${r.bottom + menu.offsetHeight + 8 > innerHeight ? r.top - menu.offsetHeight - 4 : r.bottom + 4}px`;
    setTimeout(() => document.addEventListener('click', closeMenu, { once: true }));
    addEventListener('scroll', closeMenu, { once: true, capture: true });
  }

  function card(x) {
    const { w, name, sessions, online, last, total } = x;
    const isOpen = expanded.has(name);
    const wrap = el('div', `wolts-card${online.length ? ' online' : ''}${isOpen ? ' expanded' : ''}`);
    const row = el('div', 'wolts-row');
    row.tabIndex = 0;
    row.onclick = () => { window.location.href = woltUrl(name); };
    row.onkeydown = e => { if (e.target === row && (e.key === 'Enter' || e.key === ' ')) row.click(); };

    const avatar = el('div', 'wolts-avatar');
    const sprite = woltSpriteAvatar(w.type, 36);
    if (sprite) avatar.innerHTML = sprite; else avatar.textContent = WOLT_EMOJI[w.type] || '🦫';

    const body = el('div', 'wolts-body');
    const label = woltLabel(w) || name;
    body.appendChild(el('div', 'wolts-name', label));
    const line = el('div', 'wolts-sub', `${w.type} · `);
    line.appendChild(el('span', online.length ? 'wolts-on' : '', woltStateText(x)));
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
      add.title = `New chat with ${woltLabel(w) || name}`; add.setAttribute('aria-label', add.title);
      add.onclick = e => { e.stopPropagation(); startSession(name); };
      row.appendChild(add);
    }
    const more = el('button', 'wolts-more', '⋯');
    more.title = `Manage ${woltLabel(w) || name}`; more.setAttribute('aria-label', more.title); more.setAttribute('aria-haspopup', 'menu');
    more.onclick = e => { e.stopPropagation(); openMenu(more, name, woltLabel(w) || name); };
    row.appendChild(more);
    wrap.appendChild(row);

    if (isOpen) {
      const list = el('div', 'wolts-sessions');
      sessions.slice(0, 8).forEach(s => list.appendChild(sessionRow(s, { compact: true })));
      if (total > sessions.length) {
        const all = el('a', 'wolts-session-all', `All ${total} on ${woltLabel(w) || name}'s page ›`);
        all.href = woltUrl(name);
        list.appendChild(all);
      }
      wrap.appendChild(list);
    }
    return wrap;
  }

  function archivedRow(a) {
    const shown = a.display_name || a.name, url = `/wolts/archived/${encodeURIComponent(a.name)}`;
    const row = el('div', 'wolts-archived-row');
    const body = el('div', 'wolts-body');
    body.append(el('div', 'wolts-name', shown), el('div', 'wolts-sub', a.type));
    const unarchive = el('button', 'btn btn-ghost', 'Unarchive');
    unarchive.onclick = () => archiveAction('POST', `${url}/unarchive`);
    const remove = el('button', 'btn btn-ghost', 'Delete');
    remove.onclick = () => archiveAction('DELETE', url, `Delete ${shown}? This wolt will not be recoverable.`);
    row.append(body, unarchive, remove);
    return row;
  }

  window.renderWoltsPage = function () {
    if (!view.classList.contains('active')) return;
    const all = allWolts.filter(w => WOLT_TYPES.has(w.type)).map(woltSessionSummary)
      .sort((a, b) => ((b.online.length > 0) - (a.online.length > 0)) || (b.last - a.last));
    const q = query.trim().toLowerCase();
    const hits = all.filter(x => !q || x.name.toLowerCase().includes(q) || woltLabel(x.w).toLowerCase().includes(q) || (x.w.type || '').includes(q));
    const online = hits.filter(x => x.online.length), offline = hits.filter(x => !x.online.length);
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
    const group = (label, items, showCount = true) => {
      if (!items.length) return;
      content.appendChild(el('div', 'wolts-group', showCount ? `${label} · ${items.length}` : label));
      const grid = el('div', 'wolts-grid');
      items.forEach(x => grid.appendChild(card(x)));
      content.appendChild(grid);
    };
    group('Online', online);
    group('Offline', offline, false);
    const shelved = archived.filter(a => !q || a.name.toLowerCase().includes(q) || (a.display_name || '').toLowerCase().includes(q));
    if (!hits.length && !shelved.length) content.appendChild(el('div', 'wolts-quiet', all.length ? 'No wolt matches.' : 'No wolts yet.'));
    if (shelved.length) {
      const section = el('details', 'wolts-archived');
      section.open = archivedOpen;
      section.ontoggle = () => { archivedOpen = section.open; };
      section.appendChild(el('summary', 'wolts-group', `Archived · ${shelved.length}`));
      shelved.forEach(a => section.appendChild(archivedRow(a)));
      content.appendChild(section);
    }

    view.replaceChildren(header, content);
    if (focused) { search.focus(); search.setSelectionRange(query.length, query.length); }
    refreshArchived();
  };

  // showView lives in lodge.js; draw this page whenever it becomes the active view
  const show = window.showView;
  window.showView = function (name) {
    show(name);
    if (name === 'wolts') { renderWoltsPage(); window.scrollTo(0, 0); }
  };
})();
