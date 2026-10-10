// ── Lodge Core JS ──
// Shared logic for sidebar, sessions, apps, wolts, modals.
// Requires sprites.js and navigation.js to be loaded first.

let allWolts = [];
let allApps = [];
let allSessions = [];
let lodgeSessionsLoaded = false;
let sessionTotals = {};
let appFilter = 'all';
let currentView = 'home';
const LODGE_SESSIONS_CACHE = 'woltspace:lodge-sessions:v1';

// ── Harnesses (agent engines: claude, codex, …) ──
let harnessList = [];          // [{id,label,emoji,models}]
let harnessDefault = 'claude'; // lodge default (woltspace.json harness.default)
let homeHarnessSelected = '';
let firstRun = {
  needs_harness_choice: false,
  harness_selected: false,
  has_user_wolt: true,
};

async function loadHarnesses() {
  try {
    const res = await fetch('/harnesses');
    const data = await res.json();
    harnessList = data.harnesses || [];
    harnessDefault = data.default || 'claude';
  } catch {
    harnessList = [];
  }
}

function harnessInfo(id) {
  return harnessList.find(h => h.id === id) || { id, label: id, emoji: '' };
}

// What to call a wolt where a person reads it. Its `name` stays the folder
// name used in addresses and session names.
function woltLabel(w) {
  if (!w) return '';
  return w.display_name || w.name || w.dir || '';
}

// A wolt's effective engine + the concrete model it will spawn with.
function woltHarness(w) {
  const pinned = !!w.harness;
  const id = w.harness || harnessDefault;
  const info = harnessInfo(id);
  const models = info.models || {};
  const tierDefault = models[w.type] || models.raccoon || '';  // rodent (legacy) → raccoon tier
  // per-wolt model pin wins IF valid for this engine (mirrors backend resolve_model:
  // a pin is harness-scoped, so an invalid one falls back to the tier default)
  const catalog = info.catalog || [];
  const modelPinned = !!w.model && catalog.some(c => c.id === w.model);
  const model = modelPinned ? w.model : tierDefault;
  return { id, pinned, model, modelPinned, ...info };
}

// The model an engine would use for a given tier (for picker rows).
function modelFor(harnessId, tier) {
  const m = harnessInfo(harnessId).models || {};
  return m[tier] || m.raccoon || '';
}

function modelLabelFor(harnessId, tier) {
  const harness = harnessInfo(harnessId);
  const model = modelFor(harnessId, tier);
  const catalogEntry = (harness.catalog || []).find(entry => entry.id === model);
  return catalogEntry ? catalogEntry.label : model;
}

// ── Helpers ──
function timeAgo(ts) {
  const s = Math.floor((Date.now() / 1000) - ts);
  if (s < 60) return 'just now';
  if (s < 3600) return Math.floor(s / 60) + 'm ago';
  if (s < 86400) return Math.floor(s / 3600) + 'h ago';
  return Math.floor(s / 86400) + 'd ago';
}
function lodgeElement(tag, className = '', text = '') {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== '') element.textContent = text;
  return element;
}

// One definition of session state, shared by the sidebar, Wolts page, wolt page and
// Sessions list. Online means the registry says running and the lodge view found
// its tmux window. Everything else is offline; browser time never changes state.
function sessionIsOnline(s) {
  return s.status === 'running' && s.alive === true;
}

function sessionStateText(s) {
  if (sessionIsOnline(s)) return 'online';
  const stamp = s.last_activity || s.created_at || 0;
  return stamp ? `offline · ${timeAgo(stamp)}` : 'offline';
}
const sessionActivityText = sessionStateText;

// ── View switching ──
function showView(name) {
  const target = document.getElementById(name + '-view');
  if (!target) {
    window.location.href = name === 'home' ? '/' : '/?view=' + encodeURIComponent(name);
    return;
  }
  currentView = name;
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  target.classList.add('active');
  document.querySelectorAll('.sidebar-nav-item').forEach(n => n.classList.remove('active'));
  document.getElementById('nav-' + name)?.classList.add('active');
  history.replaceState(null, '', name === 'home' ? '/' : '/?view=' + encodeURIComponent(name));
  closeSidebar();
}

// ── Sidebar ──
function toggleSidebar() {
  document.getElementById('sidebar').classList.toggle('mobile-open');
}
function closeSidebar() {
  document.getElementById('sidebar').classList.remove('mobile-open');
}
// ── Load wolts ──
async function loadWolts() {
  try {
    const [woltsResponse, onboardingResponse] = await Promise.all([
      fetch('/wolts'),
      fetch('/onboarding/status'),
    ]);
    allWolts = await woltsResponse.json();
    firstRun = await onboardingResponse.json();
    renderSidebarWolts();
    // Sessions may have rendered first, with no wolt data to name them by.
    if (allSessions.length) renderSessions();
    renderStarterWelcome();
    if (allApps.length) renderApps();
  } catch {
    document.getElementById('sidebar-wolts').innerHTML = '';
  }
  renderFirstRunHarnessChoice();
}

function renderFirstRunHarnessChoice() {
  const panel = document.getElementById('home-harness-choice');
  const options = document.getElementById('home-harness-options');
  if (!panel || !options) return;
  const needsChoice = firstRun.needs_harness_choice === true;
  panel.style.display = needsChoice ? '' : 'none';
  const cta = document.getElementById('home-create-cta');
  if (cta) {
    const starterWelcome = document.getElementById('home-starter-welcome');
    const starterVisible = starterWelcome && starterWelcome.style.display !== 'none';
    cta.style.display = !needsChoice && firstRun.has_user_wolt === false && !starterVisible ? '' : 'none';
  }
  if (!needsChoice) return;

  options.innerHTML = '';
  if (!harnessList.length) {
    document.getElementById('home-harness-status').textContent =
      'No supported harness is available yet.';
    return;
  }
  harnessList.forEach(h => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'home-harness-option';
    const name = `${h.emoji || ''} ${h.label || h.id}`.trim();
    button.appendChild(lodgeElement('span', '', name));
    button.onclick = () => chooseHomeHarness(h.id, button);
    options.appendChild(button);
  });
}

async function chooseHomeHarness(id, button) {
  homeHarnessSelected = id;
  document.querySelectorAll('.home-harness-option').forEach(el =>
    el.classList.toggle('selected', el === button));
  const status = document.getElementById('home-harness-status');
  status.textContent = 'saving…';
  document.querySelectorAll('.home-harness-option').forEach(el => { el.disabled = true; });

  try {
    const save = await fetch('/onboarding/harness', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ harness: id }),
    });
    const result = await save.json();
    if (!save.ok) throw new Error(result.error || 'could not save that choice');
    harnessDefault = id;
    if (homeHarnessSelected !== id) return;
    firstRun = result;
    renderFirstRunHarnessChoice();
    await loadWolts();
    await loadSessions();
  } catch (error) {
    if (homeHarnessSelected === id) {
      status.textContent = error.message || 'try again';
      document.querySelectorAll('.home-harness-option').forEach(el => { el.disabled = false; });
    }
  }
}

function renderStarterWelcome() {
  const card = document.getElementById('home-starter-welcome');
  if (!card) return;
  const startersOnly = allWolts.length > 0
    && allWolts.every(wolt => wolt.origin === 'starter');
  const untouched = lodgeSessionsLoaded && startersOnly && allWolts.every(wolt => {
    const name = wolt.dir || wolt.name;
    const projected = allSessions.filter(session => session.wolt === name).length;
    return (sessionTotals[name] ?? projected) === 0;
  });
  if (!untouched) {
    if (card.style.display !== 'none') {
      const quote = document.getElementById('home-quote');
      if (quote) quote.style.display = '';
    }
    card.style.display = 'none';
    card.replaceChildren();
    return;
  }

  const wolt = allWolts[0];
  const name = wolt.name || wolt.dir;
  // A fresh lodge has one thing to do: meet the starter wolt. She stands in the
  // scene with a speech bubble and a "press start" button (styles: home.css).
  // without a display name, a lowercase folder name still reads as a name
  const shown = wolt.display_name || (name.charAt(0).toUpperCase() + name.slice(1));
  const quote = document.getElementById('home-quote');
  const hero = lodgeElement('div', 'home-starter-hero');
  const bubble = lodgeElement('div', 'home-starter-bubble');
  bubble.append(`Hey! I'm ${shown}.`, document.createElement('br'), 'Want to build something?');
  hero.appendChild(bubble);
  const sprite = woltSpriteElement(wolt.type, 132);
  if (sprite) {
    const figure = lodgeElement('div', 'home-starter-sprite');
    figure.appendChild(sprite);
    hero.appendChild(figure);
  }
  const button = lodgeElement('button', 'home-starter-button', `Say hi to ${shown}`);
  button.type = 'button';
  button.addEventListener('click', () => {
    card.style.display = 'none';
    if (quote) quote.style.display = '';
    startSession(name);
  });
  hero.append(button, lodgeElement('div', 'home-starter-sub', 'your first wolt'));
  card.replaceChildren(hero);
  card.style.display = '';
  if (quote) quote.style.display = 'none';
  const cta = document.getElementById('home-create-cta');
  if (cta) cta.style.display = 'none';
}

// Sidebar = the wolts you're likely to want right now. A small lodge lists everyone;
// past SIDEBAR_ALL_UP_TO wolts it lists who is online, who you talked to
// in the last day, and the wolt you're on. Everyone else is on the Wolts page.
const SIDEBAR_ALL_UP_TO = 8;
const SIDEBAR_RECENT_SECONDS = 86400;

function woltSessionSummary(w) {
  const name = w.name || w.dir;
  const sessions = allSessions.filter(s => s.wolt === (w.dir || name))
    .sort((a, b) => (b.last_activity || b.created_at || 0) - (a.last_activity || a.created_at || 0));
  const online = sessions.filter(sessionIsOnline);
  const last = sessions.length ? (sessions[0].last_activity || sessions[0].created_at || 0) : 0;
  return {
    w, name, sessions, online, last,
    total: sessionTotals[w.dir || name] ?? sessions.length,
  };
}

// a wolt's one-word state, used wherever a wolt is listed
function woltStateText(x) {
  if (x.online.length) return 'online';
  return x.last ? `offline · ${timeAgo(x.last)}` : 'offline';
}

function renderSidebarWolts() {
  const all = allWolts.filter(w => WOLT_TYPES.has(w.type)).map(woltSessionSummary);
  const container = document.getElementById('sidebar-wolts');
  const label = document.querySelector('#sidebar-team-section .sidebar-section-label');
  const everyone = all.length <= SIDEBAR_ALL_UP_TO;
  const now = Date.now() / 1000;
  const viewing = document.body.dataset.wolt;
  let shown;
  if (everyone) {
    const tierOrder = { raccoon: 0, rodent: 0, beaver: 1, otter: 2, dog: 3 };
    shown = all.slice().sort((a, b) => (tierOrder[a.w.type] ?? 99) - (tierOrder[b.w.type] ?? 99));
  } else {
    shown = all.filter(x => x.online.length || now - x.last < SIDEBAR_RECENT_SECONDS || x.name === viewing)
      .sort((a, b) => ((b.online.length > 0) - (a.online.length > 0)) || (b.last - a.last));
  }
  if (label && label.firstChild && label.firstChild.nodeType === Node.TEXT_NODE) label.firstChild.textContent = everyone ? 'Team ' : 'Recent ';
  document.getElementById('sidebar-team-count').textContent = everyone ? (all.length || '') : '';

  container.replaceChildren();
  shown.forEach(x => {
    const { w, name, online } = x;
    const card = document.createElement('div');
    card.className = `wolt-card${viewing === name ? ' active' : ''}${online.length ? ' online' : ' offline'}`;
    card.tabIndex = 0;
    card.onclick = () => { window.location.href = `/w/${encodeURIComponent(name)}`; };
    card.onkeydown = e => { if (e.target === card && (e.key === 'Enter' || e.key === ' ')) card.click(); };
    const avatar = document.createElement('div'); avatar.className = 'wolt-avatar';
    const sprite = woltSpriteAvatar(w.type, 36);
    if (sprite) avatar.innerHTML = sprite; else avatar.textContent = WOLT_EMOJI[w.type] || '🦫';
    if (online.length) {
      const dot = document.createElement('div');
      dot.className = 'wolt-status-dot running';
      dot.title = 'online';
      avatar.appendChild(dot);
    }
    const info = document.createElement('div'); info.className = 'wolt-info';
    const nameEl = document.createElement('div'); nameEl.className = 'wolt-name'; nameEl.textContent = woltLabel(w) || name;
    const sub = document.createElement('div'); sub.className = 'wolt-type';
    sub.textContent = everyone ? w.type : woltStateText(x);
    info.append(nameEl, sub); card.append(avatar, info);
    if (RODENT_TYPES.has(w.type)) { const add = document.createElement('button'); add.className = 'wolt-quick-session'; add.textContent = '+'; add.title = `New session with ${name}`; add.setAttribute('aria-label', add.title); add.onclick = e => { e.stopPropagation(); startSession(name); }; card.appendChild(add); }
    container.appendChild(card);
  });
  if (!everyone) {
    if (!shown.length) {
      const quiet = document.createElement('div'); quiet.className = 'sidebar-wolts-quiet'; quiet.textContent = 'Everyone is offline.';
      container.appendChild(quiet);
    }
    const more = document.createElement('a');
    more.className = 'sidebar-all-wolts'; more.href = '/?view=wolts';
    more.textContent = `All ${all.length} wolts ›`;
    more.onclick = e => { if (document.getElementById('wolts-view')) { e.preventDefault(); showView('wolts'); } };
    container.appendChild(more);
  }
  const woltsViewBusy = document.querySelector('#wolts-view .session-control[data-mode]');
  if (typeof renderWoltsPage === 'function' && !woltsViewBusy) renderWoltsPage();
}

// ── Engine picker (per-wolt harness override) ──
function closeEnginePicker() {
  const p = document.getElementById('engine-pop');
  if (p) p.remove();
  document.removeEventListener('click', closeEnginePicker);
}

// Dedicated handler so a chip click can never fall through to the card's
// startSession() (which would spawn a session).
function engineChipClick(ev, anchorEl, name) {
  ev.stopPropagation();
  ev.preventDefault();
  openEnginePicker(anchorEl, name);
}

function openEnginePicker(anchorEl, name) {
  const existing = document.getElementById('engine-pop');
  closeEnginePicker();
  if (existing && existing.dataset.wolt === name) return;  // click again to toggle closed

  const w = allWolts.find(x => (x.name || x.dir) === name);
  if (!w) return;
  const effective = w.harness || harnessDefault;   // engine this wolt runs now

  const pop = document.createElement('div');
  pop.id = 'engine-pop';
  pop.className = 'engine-pop';
  pop.dataset.wolt = name;
  pop.appendChild(lodgeElement('div', 'engine-pop-head', 'Engine'));
  harnessList.forEach(h => {
    const selected = h.id === effective;
    const button = lodgeElement('button', `engine-opt${selected ? ' sel' : ''}`);
    button.type = 'button';
    button.appendChild(lodgeElement('span', 'engine-radio', selected ? '●' : '○'));
    button.appendChild(lodgeElement('span', 'engine-opt-label', h.label));
    const model = modelFor(h.id, w.type);
    const sub = model + (h.id === harnessDefault ? ' · default' : '');
    if (sub) button.appendChild(lodgeElement('span', 'engine-opt-sub', sub));
    button.addEventListener('click', event => {
      event.stopPropagation();
      setWoltHarness(name, h.id);
    });
    pop.appendChild(button);
  });
  pop.appendChild(lodgeElement(
    'div', 'engine-pop-note',
    'Applies to the next session — the one running now keeps its engine.',
  ));
  pop.addEventListener('click', e => e.stopPropagation());
  document.body.appendChild(pop);

  // Anchor to the chip, right-aligned; flip above if it would overflow the viewport.
  const r = anchorEl.getBoundingClientRect();
  let left = Math.min(r.right - pop.offsetWidth, window.innerWidth - pop.offsetWidth - 8);
  let top = r.bottom + 6;
  if (top + pop.offsetHeight > window.innerHeight - 8) top = r.top - pop.offsetHeight - 6;
  pop.style.left = Math.max(8, left) + 'px';
  pop.style.top = Math.max(8, top) + 'px';

  setTimeout(() => document.addEventListener('click', closeEnginePicker), 0);
}

async function setWoltHarness(name, harness) {
  try {
    const res = await fetch(`/wolts/${encodeURIComponent(name)}/harness`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ harness }),
    });
    if (!res.ok) throw new Error('failed');
    const w = allWolts.find(x => (x.name || x.dir) === name);
    if (w) { if (harness) w.harness = harness; else delete w.harness; }
    renderSidebarWolts();
  } catch {
    /* leave UI as-is on failure */
  } finally {
    closeEnginePicker();
  }
}

// ── Load apps ──
async function loadApps() {
  try {
    const res = await fetch('/apps');
    allApps = await res.json();
    renderApps();
  } catch {
    document.getElementById('app-grid').innerHTML =
      '<div class="empty-state"><div class="empty-state-icon">📦</div><div class="empty-state-text">failed to load apps</div></div>';
  }
}

function renderApps() {
  const filtered = appFilter === 'all'
    ? allApps
    : appFilter === 'running'
      ? allApps.filter(p => p.running)
      : allApps.filter(p => !p.running);

  const runCount = allApps.filter(p => p.running).length;
  document.getElementById('apps-subtitle').textContent =
    `${allApps.length} app${allApps.length !== 1 ? 's' : ''} · ${runCount} running`;

  const grid = document.getElementById('app-grid');
  if (!filtered.length && allApps.length) {
    grid.innerHTML = '<div class="empty-state"><div class="empty-state-icon">🔍</div><div class="empty-state-text">no matching apps</div></div>';
    return;
  }
  if (!filtered.length) {
    grid.innerHTML = '<div class="empty-state"><div class="empty-state-icon">📦</div><div class="empty-state-text">no apps yet</div></div>';
    return;
  }

  grid.replaceChildren(...filtered.map(p => {
    const card = lodgeElement('div', 'app-card');
    card.setAttribute('role', 'link');
    card.tabIndex = 0;
    const destination = p.running
      ? WoltspaceNavigation.appDestination(p)
      : `/a/${encodeURIComponent(p.name)}`;
    card.addEventListener('click', () => WoltspaceNavigation.internal(destination));
    card.addEventListener('keydown', event => {
      if (event.target !== card || !['Enter', ' '].includes(event.key)) return;
      event.preventDefault();
      WoltspaceNavigation.internal(destination);
    });

    const status = p.running ? 'running' : 'stopped';
    const canToggle = !!p.start;
    const keeper = p.keeper || 'unassigned';
    const keeperWolt = allWolts.find(w => (w.name || w.dir) === keeper);
    const keeperEmoji = keeperWolt ? (WOLT_EMOJI[keeperWolt.type] || '🦫') : '📦';
    const keeperSprite = keeperWolt ? woltSpriteAvatar(keeperWolt.type, 24) : null;
    const body = lodgeElement('div', 'app-card-body');
    const top = lodgeElement('div', 'app-card-top');
    top.appendChild(lodgeElement('span', 'app-emoji', p.emoji || '📦'));
    const topRight = lodgeElement('div', 'ma-topright');
    topRight.appendChild(lodgeElement('span', 'ma-share', '🔒 Just me'));
    const statusElement = lodgeElement('div', `app-status ${status}`, status);
    statusElement.prepend(lodgeElement('div', 'app-status-dot'));
    topRight.appendChild(statusElement); top.appendChild(topRight); body.appendChild(top);

    const nameElement = lodgeElement(p.running ? 'a' : 'div', 'app-name-link', p.name);
    if (p.running) nameElement.href = WoltspaceNavigation.appDestination(p);
    nameElement.addEventListener('click', event => event.stopPropagation());
    body.appendChild(nameElement);
    if (p.stack) {
      const stack = lodgeElement('div', 'app-stack');
      stack.appendChild(lodgeElement('span', 'stack-tag', p.stack));
      body.appendChild(stack);
    }
    body.appendChild(lodgeElement('div', 'app-desc', p.description || 'No description'));

    const footer = lodgeElement('div', 'app-card-footer');
    const keeperButton = lodgeElement('div', 'app-wolt keeper-btn');
    keeperButton.title = `open with ${keeper}`;
    keeperButton.addEventListener('click', event => {
      event.stopPropagation(); openApp(p.name, keeper);
    });
    const avatar = lodgeElement('div', 'app-wolt-avatar');
    if (keeperSprite) avatar.innerHTML = keeperSprite; else avatar.textContent = keeperEmoji;
    keeperButton.appendChild(avatar);
    const keeperText = lodgeElement('div');
    keeperText.appendChild(lodgeElement('div', 'app-wolt-name', keeper));
    keeperText.appendChild(lodgeElement(
      'div', p.source ? 'app-wolt-assign app-source-link' : 'app-wolt-assign',
      p.source ? `⎋ ${p.source.replace('https://github.com/', '')}` : 'keeper',
    ));
    keeperButton.appendChild(keeperText); footer.appendChild(keeperButton);

    const actions = lodgeElement('div', 'app-actions');
    if (canToggle) {
      const toggle = lodgeElement(
        'button', `tool-button ${p.running ? 'danger' : 'primary'}`,
        p.running ? '■ Stop' : '▶ Start',
      );
      toggle.type = 'button'; toggle.title = p.running ? 'Stop' : 'Start';
      toggle.addEventListener('click', event => {
        event.stopPropagation(); toggleApp(p.name, p.running);
      });
      actions.appendChild(toggle);
    }
    const settings = lodgeElement('a', 'tool-button icon-button', '⚙');
    settings.href = `/a/${encodeURIComponent(p.name)}`;
    settings.setAttribute('aria-label', `${p.name} settings`);
    settings.addEventListener('click', event => event.stopPropagation());
    actions.appendChild(settings); footer.appendChild(actions); body.appendChild(footer);
    card.appendChild(body);
    return card;
  }));
}

function filterApps(filter, el) {
  appFilter = filter;
  document.querySelectorAll('#app-filters .filter-chip').forEach(c => c.classList.remove('active'));
  if (el) el.classList.add('active');
  renderApps();
}

async function toggleApp(name, isRunning) {
  const action = isRunning ? 'stop' : 'start';
  try {
    await fetch(`/apps/${name}/${action}`, { method: 'POST' });
    await loadApps();
  } catch {}
}

async function toggleShare(name, isSharing) {
  const action = isSharing ? 'unshare' : 'share';
  const btn = document.querySelector(`.action-btn.${isSharing ? 'shared' : 'share'}`);
  if (btn) { btn.disabled = true; btn.textContent = '⏳'; }
  try {
    const res = await fetch(`/apps/${name}/${action}`, { method: 'POST' });
    const data = await res.json();
    if (data.tunnel_url) {
      await navigator.clipboard.writeText(data.tunnel_url).catch(() => {});
      if (btn) { btn.textContent = '✅'; }
      await new Promise(r => setTimeout(r, 1200));
    }
    await loadApps();
  } catch {
    if (btn) { btn.textContent = '❌'; }
    await new Promise(r => setTimeout(r, 1000));
    await loadApps();
  }
}

// ── Load sessions ──
async function loadSessions() {
  try {
    const res = await fetch('/sessions?view=lodge');
    const payload = await res.json();
    applyLodgeSessions(payload, true);
  } catch {
    const list = document.getElementById('sessions-list');
    if (list) list.innerHTML =
      '<div class="empty-state"><div class="empty-state-icon">🌿</div><div class="empty-state-text">failed to load sessions</div></div>';
  }
}

function applyLodgeSessions(payload, persist = false) {
  const sessions = Array.isArray(payload) ? payload : payload?.sessions;
  if (!Array.isArray(sessions)) return;
  allSessions = sessions;
  if (persist) lodgeSessionsLoaded = true;
  sessionTotals = payload && !Array.isArray(payload) && payload.totals
    ? payload.totals : {};
  if (persist) {
    try {
      sessionStorage.setItem(LODGE_SESSIONS_CACHE, JSON.stringify({
        sessions: allSessions, totals: sessionTotals,
      }));
    } catch {}
  }
  renderSidebarWolts();
  renderSessions();
  renderStarterWelcome();
}

function restoreLodgeSessions() {
  try {
    const cached = JSON.parse(sessionStorage.getItem(LODGE_SESSIONS_CACHE) || 'null');
    applyLodgeSessions(cached);
  } catch {}
}

// Sessions can render before /wolts answers, so a group's avatar and name are
// repainted on every render, not only when the group is created. The avatar is
// rewritten only when the wolt's type changes, so the 15s poll doesn't flicker.
function paintSessionGroupHeader(group, wolt) {
  const woltData = allWolts.find(w => (w.name || w.dir) === wolt);
  const type = woltData ? woltData.type || '' : '';
  const avatar = group.querySelector('.sessions-group-avatar');
  if (avatar && avatar.dataset.type !== type) {
    const sprite = woltData ? woltSpriteAvatar(woltData.type, 20) : null;
    if (sprite) avatar.innerHTML = sprite; else avatar.textContent = WOLT_EMOJI[type] || '🦫';
    avatar.dataset.type = type;
  }
  const label = group.querySelector('.sessions-group-name');
  if (label) label.textContent = woltLabel(woltData) || wolt;
}

function renderSessions() {
  const online = allSessions.filter(s => s.name !== 'main' && sessionIsOnline(s));
  const total = Object.values(sessionTotals).reduce((sum, count) => sum + count, 0)
    || allSessions.length;
  const subtitle = document.getElementById('sessions-subtitle');
  if (subtitle) subtitle.textContent = `${online.length} online · ${total} total`;
  const badge = document.getElementById('sessions-badge');
  if (badge) {
    badge.textContent = online.length || '';
    badge.classList.toggle('visible', online.length > 0);
  }
  const container = document.getElementById('sessions-list');
  if (!container) return;

  const byName = new Map(allSessions.map(session => [session.name, session]));
  container.querySelectorAll('.session-row[data-session]').forEach(row => {
    const session = byName.get(row.dataset.session);
    if (session) updateSessionRow(row, session);
  });
  const groups = new Map();
  online.forEach(session => {
    const name = session.wolt || 'unknown';
    if (!groups.has(name)) groups.set(name, []);
    groups.get(name).push(session);
  });
  if (online.length) container.querySelector('.empty-state')?.remove();
  [...groups.entries()].sort(([a], [b]) => a.localeCompare(b)).forEach(([wolt, sessions]) => {
    let group = [...container.querySelectorAll('.sessions-group')].find(node => node.dataset.wolt === wolt);
    if (!group) {
      group = lodgeElement('div', 'sessions-group'); group.dataset.wolt = wolt;
      const header = lodgeElement('div', 'sessions-group-header');
      header.append(lodgeElement('div', 'sessions-group-avatar'), lodgeElement('span', 'sessions-group-name'), lodgeElement('span', 'sessions-group-meta'));
      const chevron = lodgeElement('span', 'sessions-group-chevron', '⌄');
      header.appendChild(chevron);
      header.addEventListener('click', () => toggleSessionGroup(header));
      const body = lodgeElement('div', 'sessions-group-body');
      body.appendChild(lodgeElement('div', 'sessions-group-inner'));
      group.append(header, body);
      const next = [...container.querySelectorAll('.sessions-group')]
        .find(node => node.dataset.wolt.localeCompare(wolt) > 0);
      container.insertBefore(group, next || null);
    }
    paintSessionGroupHeader(group, wolt);
    syncSessionRows(group.querySelector('.sessions-group-inner'), sessions);
  });
  container.querySelectorAll('.sessions-group').forEach(group => {
    const wolt = group.dataset.wolt;
    const count = online.filter(session => (session.wolt || 'unknown') === wolt).length;
    const total = sessionTotals[wolt] ?? group.querySelectorAll('.session-row').length;
    group.querySelector('.sessions-group-meta').textContent = `${count} online · ${total} total`;
  });
  if (!online.length && !container.querySelector('.sessions-group')) {
    const empty = lodgeElement('div', 'empty-state');
    empty.append(lodgeElement('div', 'empty-state-icon', '🌿'), lodgeElement('div', 'empty-state-text', 'no sessions online'));
    container.replaceChildren(empty);
  }
}

// ── Session group toggle ──
function toggleSessionGroup(header) {
  header.querySelector('.sessions-group-chevron').classList.toggle('collapsed');
  header.nextElementSibling.classList.toggle('collapsed');
}

// ── Start session ──
function startSession(woltName) {
  fetch('/sessions/new/lodge', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ wolt: woltName }),
  }).then(r => r.json()).then(data => {
    if (data.name) WoltspaceNavigation.internal('/tui?session=' + encodeURIComponent(data.name));
  }).catch(() => {});
}

// ── Open app ──
function openApp(appName, keeper) {
  fetch('/sessions/new/lodge', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      wolt: keeper,
      app: appName,
      prompt: `You're working on the "${appName}" app. The viewport is showing /app/${appName}/, not your personal site.`,
    }),
  }).then(r => r.json()).then(data => {
    if (data.name) WoltspaceNavigation.internal('/tui?session=' + encodeURIComponent(data.name));
  }).catch(() => {});
}

// ── Create wolt modal ──
let createSelectedType = null;
let createSelectedHarness = '';

// Naming a wolt is the hard part: the empty name field cycles a few ideas.
const CREATE_NAME_IDEAS = [
  'Wolt Disney', 'Justin Beaver', 'Wolter White', 'Racoona Matata',
  'George Coony', 'Beaverly Hills',
];
let createNameIdeaTimer = null;

function startCreateNameIdeas() {
  const input = document.getElementById('create-name');
  if (!input) return;
  stopCreateNameIdeas();
  let index = Math.floor(Math.random() * CREATE_NAME_IDEAS.length);
  const show = () => {
    input.placeholder = CREATE_NAME_IDEAS[index % CREATE_NAME_IDEAS.length];
    index += 1;
  };
  show();
  createNameIdeaTimer = setInterval(show, 2400);
}

function stopCreateNameIdeas() {
  if (createNameIdeaTimer) clearInterval(createNameIdeaTimer);
  createNameIdeaTimer = null;
}

// The name as typed. The lodge decides what it becomes when the wolt is created;
// this page never works that out itself.
function createWoltName() {
  return document.getElementById('create-name').value.trim();
}

// A name is taken when it reads the same as a wolt already in the lodge.
// This only warns early; the create request is still the judge.
function sameWoltName(a, b) {
  const plain = value => String(value || '').toLowerCase().replace(/[^a-z0-9]+/g, '');
  return !!plain(a) && plain(a) === plain(b);
}

function createNameWarning() {
  const typed = createWoltName();
  if (!typed) return '';
  const taken = allWolts.find(w => sameWoltName(typed, w.name) || sameWoltName(typed, w.display_name));
  return taken ? `${woltLabel(taken)} already lives here - pick another name` : '';
}

function showCreateNameWarning(text) {
  const line = document.getElementById('create-name-warning');
  const input = document.getElementById('create-name');
  line.textContent = text;
  line.hidden = !text;
  input.classList.toggle('invalid', !!text);
  input.setAttribute('aria-invalid', text ? 'true' : 'false');
}

function openCreateWolt(e) {
  if (e) e.preventDefault();
  document.getElementById('create-modal').classList.add('open');
  document.getElementById('create-name').value = '';
  showCreateNameWarning('');
  // One working style is always picked; the beaver is the starting choice.
  createSelectedType = 'beaver';
  createSelectedHarness = harnessDefault;
  document.querySelectorAll('.type-card').forEach(c => {
    c.classList.toggle('selected', c.dataset.type === createSelectedType);
  });
  document.getElementById('create-submit').disabled = false;
  document.getElementById('create-submit').textContent = 'Create';
  document.getElementById('create-error').style.display = 'none';
  renderCreateHarnessOptions();
  startCreateNameIdeas();
  setTimeout(() => document.getElementById('create-name').focus(), 50);
}

function closeCreateWolt() {
  document.getElementById('create-modal').classList.remove('open');
  stopCreateNameIdeas();
}

function pickType(el) {
  document.querySelectorAll('.type-card').forEach(c => c.classList.remove('selected'));
  el.classList.add('selected');
  createSelectedType = el.dataset.type;
  updateCreatePreview();
}

function renderCreateHarnessOptions() {
  const select = document.getElementById('create-harness');
  if (!select) return;
  select.innerHTML = '';
  harnessList.forEach(harness => {
    const option = document.createElement('option');
    option.value = harness.id;
    option.textContent = `${harness.label || harness.id}`
      + (harness.id === harnessDefault ? ' (lodge default)' : '');
    option.selected = harness.id === createSelectedHarness;
    select.appendChild(option);
  });
  // A stale/default id absent from the registry should never submit silently.
  if (!harnessList.some(h => h.id === createSelectedHarness)) {
    const first = harnessList[0];
    createSelectedHarness = first ? first.id : '';
    select.value = createSelectedHarness;
  }
  renderCreateHarness();
}

function selectCreateHarness(id) {
  createSelectedHarness = id;
  renderCreateHarness();
  updateCreatePreview();
}

function renderCreateHarness() {
  const harness = harnessInfo(createSelectedHarness);
  const catalog = harness.catalog || [];
  // Engines that take any typed model name have no fixed list to choose from.
  const choosable = !harness.freeform_model && catalog.length > 0;
  document.querySelectorAll('.type-card').forEach(card => {
    const hint = card.querySelector('.type-card-hint');
    const model = card.querySelector('.type-card-model');
    const select = card.querySelector('.type-card-select');
    const usual = modelFor(createSelectedHarness, card.dataset.type);
    if (hint) hint.textContent = hint.dataset.pace || '';
    if (model) {
      model.textContent = modelLabelFor(createSelectedHarness, card.dataset.type);
      model.hidden = choosable;
    }
    if (!select) return;
    select.hidden = !choosable;
    select.replaceChildren();
    if (!choosable) return;
    catalog.forEach(entry => {
      const option = document.createElement('option');
      option.value = entry.id;
      option.textContent = entry.label || entry.id;
      select.appendChild(option);
    });
    // A default the list no longer offers still shows, instead of silently
    // displaying some other model as selected.
    if (usual && !catalog.some(entry => entry.id === usual)) {
      const option = document.createElement('option');
      option.value = usual;
      option.textContent = usual;
      select.prepend(option);
    }
    select.value = usual;
    select.dataset.usual = usual;
  });
}

// The model to pin at creation: only a choice that differs from the working
// style's usual model. Leaving the dropdown alone keeps following the default.
function createSelectedModel() {
  const card = document.querySelector(`.type-card[data-type="${createSelectedType}"]`);
  const select = card && card.querySelector('.type-card-select');
  if (!select || select.hidden || !select.value) return '';
  return select.value === select.dataset.usual ? '' : select.value;
}

document.querySelectorAll('.type-card-select').forEach(select => {
  // Choosing a model on a card also chooses that card; the click must not
  // bubble into the card's own handler twice.
  select.addEventListener('click', event => {
    event.stopPropagation();
    pickType(select.closest('.type-card'));
  });
  select.addEventListener('change', () => pickType(select.closest('.type-card')));
});

function updateCreatePreview() {
  document.getElementById('create-error').style.display = 'none';
  showCreateNameWarning(createNameWarning());
}

// Create is never a dead button: pressed too early, it says what is missing.
async function submitCreateWolt() {
  const name = createWoltName();
  const warning = name ? createNameWarning() : 'give your wolt a name first';
  if (warning) {
    showCreateNameWarning(warning);
    document.getElementById('create-name').focus();
    return;
  }
  if (!createSelectedType || !createSelectedHarness) return;

  const submit = document.getElementById('create-submit');
  const error = document.getElementById('create-error');
  submit.textContent = 'Creating...';
  submit.disabled = true;
  error.style.display = 'none';

  try {
    const res = await fetch('/sessions/new/create', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name,
        type: createSelectedType,
        harness: createSelectedHarness,
        ...(createSelectedModel() ? { model: createSelectedModel() } : {}),
      }),
    });
    const data = await res.json();
    if (!res.ok) {
      error.textContent = data.detail || 'failed to create wolt';
      error.style.display = 'block';
      submit.textContent = 'Create';
      submit.disabled = false;
      return;
    }
    closeCreateWolt();
    if (data.name) WoltspaceNavigation.internal('/tui?session=' + encodeURIComponent(data.name));
    loadWolts();
  } catch (e) {
    error.textContent = 'network error — try again';
    error.style.display = 'block';
    submit.textContent = 'Create';
    submit.disabled = false;
  }
}

// ── Keyboard ──
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeCreateWolt();
});

// ── Init ──
// Replace type card emoji with pixel art sprites
document.querySelectorAll('.type-card').forEach(card => {
  const type = card.dataset.type;
  const sprite = woltSpriteAvatar(type, 40);
  if (sprite) card.querySelector('.type-card-emoji').innerHTML = sprite;
});

restoreLodgeSessions();
loadHarnesses().finally(loadWolts);
if (document.getElementById('app-grid')) loadApps();
loadSessions();
// keep the sidebar's signals fresh; skip while the tab is hidden
setInterval(() => { if (!document.hidden) loadSessions(); }, 15000);

const requestedView = new URLSearchParams(window.location.search).get('view');
if (requestedView && ['home', 'apps', 'sessions', 'wolts'].includes(requestedView)) showView(requestedView);

console.log('%c🦫', 'font-size:3rem');
console.log('%cwoltspace — the lodge', 'color:#C98B2A;font-family:monospace');

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}
