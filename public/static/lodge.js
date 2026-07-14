// ── Lodge Core JS ──
// Shared logic for sidebar, sessions, apps, wolts, modals.
// Requires sprites.js to be loaded first.

let allWolts = [];
let allApps = [];
let allSessions = [];
let appFilter = 'all';
let currentView = 'home';

// ── Harnesses (agent engines: claude, codex, …) ──
let harnessList = [];          // [{id,label,emoji,models}]
let harnessDefault = 'claude'; // lodge default (woltspace.json harness.default)

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

// A wolt's effective engine + the concrete model for its tier (creature type).
function woltHarness(w) {
  const pinned = !!w.harness;
  const id = w.harness || harnessDefault;
  const info = harnessInfo(id);
  const models = info.models || {};
  const model = models[w.type] || models.raccoon || '';  // rodent (legacy) → raccoon tier
  return { id, pinned, model, ...info };
}

// The model an engine would use for a given tier (for picker rows).
function modelFor(harnessId, tier) {
  const m = harnessInfo(harnessId).models || {};
  return m[tier] || m.raccoon || '';
}

// ── Helpers ──
function timeAgo(ts) {
  const s = Math.floor((Date.now() / 1000) - ts);
  if (s < 60) return 'just now';
  if (s < 3600) return Math.floor(s / 60) + 'm ago';
  if (s < 86400) return Math.floor(s / 3600) + 'h ago';
  return Math.floor(s / 86400) + 'd ago';
}

// ── View switching ──
function showView(name) {
  currentView = name;
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.getElementById(name + '-view').classList.add('active');
  document.querySelectorAll('.sidebar-nav-item').forEach(n => n.classList.remove('active'));
  document.getElementById('nav-' + name).classList.add('active');
  closeSidebar();
}

// ── Sidebar ──
function toggleSidebar() {
  document.getElementById('sidebar').classList.toggle('mobile-open');
}
function closeSidebar() {
  document.getElementById('sidebar').classList.remove('mobile-open');
}
function toggleCreatures() {
  document.getElementById('creatures-list').classList.toggle('open');
  document.getElementById('creatures-chevron').classList.toggle('open');
}

// ── Load wolts ──
async function loadWolts() {
  try {
    const res = await fetch('/wolts');
    allWolts = await res.json();
    renderSidebarWolts();
  } catch {
    document.getElementById('sidebar-wolts').innerHTML = '';
  }
  // Show "create your first wolt" CTA when no wolts exist
  const cta = document.getElementById('home-create-cta');
  if (cta) cta.style.display = allWolts.length === 0 ? '' : 'none';
}

function renderSidebarWolts() {
  const chatWolts = allWolts.filter(w => WOLT_TYPES.has(w.type));
  const tierOrder = { raccoon: 0, rodent: 0, beaver: 1, otter: 2, dog: 3 };
  chatWolts.sort((a, b) => (tierOrder[a.type] ?? 99) - (tierOrder[b.type] ?? 99));

  document.getElementById('sidebar-team-count').textContent = chatWolts.length || '';

  const container = document.getElementById('sidebar-wolts');
  if (!chatWolts.length) {
    container.innerHTML = '';
    return;
  }

  const woltsWithSessions = new Set();
  allSessions.forEach(s => {
    if (s.status === 'running' && s.wolt) woltsWithSessions.add(s.wolt);
  });

  container.innerHTML = chatWolts.map(w => {
    const emoji = WOLT_EMOJI[w.type] || '🦫';
    const name = w.name || w.dir;
    const isRunning = woltsWithSessions.has(w.dir || name);
    const statusClass = isRunning ? 'running' : '';
    const isRodent = RODENT_TYPES.has(w.type);
    const role = w.role || '';
    const desc = w.description || '';
    const eng = woltHarness(w);
    const tooltip = (role || desc) ? `
      <div class="wolt-tooltip">
        <div class="wolt-tooltip-name">${emoji} ${name}</div>
        ${role ? `<div class="wolt-tooltip-role">${role}</div>` : ''}
        ${desc ? `<div class="wolt-tooltip-desc">${desc}</div>` : ''}
      </div>` : '';
    const spriteHtml = woltSpriteAvatar(w.type, 36);
    // Engine chip: a small mono tag, hidden at rest and revealed on card hover;
    // a pinned override stays visible (a deliberate divergence is worth surfacing).
    const engChip = isRodent
      ? `<button class="wolt-engine-btn${eng.pinned ? ' pinned' : ''}" title="${eng.label}${eng.model ? ' · ' + eng.model : ''}${eng.pinned ? '' : ' (lodge default)'} — change" aria-label="Change engine for ${name}" onclick="engineChipClick(event, this, '${name}')"><span class="eng-name">${eng.id}</span>${eng.model ? `<span class="eng-model">${eng.model}</span>` : ''}</button>`
      : '';
    return `<div class="wolt-card" onclick="${isRodent ? `startSession('${name}')` : ''}">
      <div class="wolt-avatar">
        ${spriteHtml || emoji}
        <div class="wolt-status-dot ${statusClass}"></div>
      </div>
      <div class="wolt-info">
        <div class="wolt-name">${name}</div>
        <div class="wolt-type">${w.type}</div>
        ${tooltip}
      </div>
      ${engChip}
    </div>`;
  }).join('');
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

  const row = (id, label, sub, selected) => `
    <button class="engine-opt${selected ? ' sel' : ''}" onclick="event.stopPropagation();setWoltHarness('${name}', '${id}')">
      <span class="engine-radio">${selected ? '●' : '○'}</span>
      <span class="engine-opt-label">${label}</span>
      ${sub ? `<span class="engine-opt-sub">${sub}</span>` : ''}
    </button>`;

  const opts = harnessList
    .map(h => {
      const model = modelFor(h.id, w.type);
      const sub = model + (h.id === harnessDefault ? ' · default' : '');
      return row(h.id, h.label, sub, h.id === effective);
    })
    .join('');

  const pop = document.createElement('div');
  pop.id = 'engine-pop';
  pop.className = 'engine-pop';
  pop.dataset.wolt = name;
  pop.innerHTML = `
    <div class="engine-pop-head">Engine</div>
    ${opts}
    <div class="engine-pop-note">Applies to the next session — the one running now keeps its engine.</div>`;
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

  grid.innerHTML = filtered.map(p => {
    const emoji = p.emoji || '📦';
    const desc = p.description || 'No description';
    const status = p.running ? 'running' : 'stopped';
    const canToggle = !!p.start;
    const keeper = p.keeper || 'unassigned';
    const keeperWolt = allWolts.find(w => (w.name || w.dir) === keeper);
    const keeperEmoji = keeperWolt ? (WOLT_EMOJI[keeperWolt.type] || '🦫') : '📦';
    const keeperSprite = keeperWolt ? woltSpriteAvatar(keeperWolt.type, 24) : null;
    const stackTags = p.stack ? `<span class="stack-tag">${p.stack}</span>` : '';
    const sourceLink = p.source ? `<a class="app-source-link" href="${p.source}" target="_blank" onclick="event.stopPropagation()">⎋ ${p.source.replace('https://github.com/', '')}</a>` : '';

    const appUrl = 'http://' + p.name + '.localhost:7777/';

    return `<div class="app-card" onclick="${p.running ? `window.open('${appUrl}','_blank')` : ''}">
      <div class="app-card-body">
        <div class="app-card-top">
          <span class="app-emoji">${emoji}</span>
          <div class="app-status ${status}">
            <div class="app-status-dot"></div>
            ${status}
          </div>
        </div>
        <div class="app-name-link">${p.name}</div>
        ${stackTags ? `<div class="app-stack">${stackTags}</div>` : ''}
        <div class="app-desc">${desc}</div>
        <div class="app-card-footer">
          <div class="app-wolt keeper-btn" title="open with ${keeper}" onclick="event.stopPropagation();openApp('${p.name}','${keeper}')">
            <div class="app-wolt-avatar">${keeperSprite || keeperEmoji}</div>
            <div>
              <div class="app-wolt-name">${keeper}</div>
              <div class="app-wolt-assign">${sourceLink || 'keeper'}</div>
            </div>
          </div>
          <div class="app-actions">
            ${canToggle ? `<button class="action-btn ${p.running ? 'stop' : 'start'}" title="${p.running ? 'Stop' : 'Start'}" onclick="event.stopPropagation();toggleApp('${p.name}', ${p.running})">${p.running ? '■' : '▶'}</button>` : ''}
          </div>
        </div>
      </div>
    </div>`;
  }).join('');
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
    const res = await fetch('/sessions');
    allSessions = await res.json();
    renderSessions();
    renderSidebarWolts();
  } catch {
    document.getElementById('sessions-list').innerHTML =
      '<div class="empty-state"><div class="empty-state-icon">🌿</div><div class="empty-state-text">failed to load sessions</div></div>';
  }
}

function renderSessions() {
  const running = allSessions.filter(s => s.name !== 'main' && s.status === 'running');
  document.getElementById('sessions-subtitle').textContent =
    `${running.length} running · ${allSessions.length} total`;
  const badge = document.getElementById('sessions-badge');
  if (running.length > 0) {
    badge.textContent = running.length;
    badge.classList.add('visible');
  } else {
    badge.classList.remove('visible');
  }

  const woltNames = [...new Set(allSessions.map(s => s.wolt).filter(Boolean))];
  const tabs = document.getElementById('sessions-filter-tabs');
  tabs.innerHTML = `<button class="filter-chip active" onclick="sessionFilterWolt=null;filterSessions();this.parentElement.querySelectorAll('.filter-chip').forEach(c=>c.classList.remove('active'));this.classList.add('active')">All</button>`
    + woltNames.map(w => {
      const emoji = WOLT_EMOJI[allWolts.find(wo => (wo.name || wo.dir) === w)?.type] || '🦫';
      return `<button class="filter-chip" onclick="sessionFilterWolt='${w}';filterSessions();this.parentElement.querySelectorAll('.filter-chip').forEach(c=>c.classList.remove('active'));this.classList.add('active')">${emoji} ${w}</button>`;
    }).join('');

  filterSessions();
}

let sessionFilterWolt = null;

function filterSessions() {
  const search = document.getElementById('sessions-search').value.toLowerCase();
  const sort = document.getElementById('sessions-sort').value;

  let filtered = allSessions.filter(s => s.name !== 'main');
  if (runningOnly) filtered = filtered.filter(s => s.status === 'running' && s.alive !== false);
  if (sessionFilterWolt) filtered = filtered.filter(s => s.wolt === sessionFilterWolt);
  if (search) filtered = filtered.filter(s =>
    (s.name || '').toLowerCase().includes(search) ||
    (s.wolt || '').toLowerCase().includes(search) ||
    (s.title || '').toLowerCase().includes(search)
  );

  if (sort === 'name') {
    filtered.sort((a, b) => (a.name || '').localeCompare(b.name || ''));
  } else {
    filtered.sort((a, b) => {
      const aR = a.status === 'running' ? 0 : 1;
      const bR = b.status === 'running' ? 0 : 1;
      if (aR !== bR) return aR - bR;
      return (b.created_at || 0) - (a.created_at || 0);
    });
  }

  const groups = {};
  filtered.forEach(s => {
    const w = s.wolt || 'unknown';
    if (!groups[w]) groups[w] = [];
    groups[w].push(s);
  });

  const container = document.getElementById('sessions-list');
  if (!filtered.length) {
    container.innerHTML = '<div class="empty-state"><div class="empty-state-icon">🌿</div><div class="empty-state-text">no sessions found</div></div>';
    return;
  }

  container.innerHTML = Object.entries(groups).map(([wolt, sessions]) => {
    const woltData = allWolts.find(w => (w.name || w.dir) === wolt);
    const emoji = woltData ? (WOLT_EMOJI[woltData.type] || '🦫') : '🦫';
    const sessionSprite = woltData ? woltSpriteAvatar(woltData.type, 20) : null;
    const runCount = sessions.filter(s => s.status === 'running' && s.alive !== false).length;
    const metaText = runCount > 0
      ? `${runCount} running · ${sessions.length} total`
      : `${sessions.length} session${sessions.length !== 1 ? 's' : ''}`;
    const rows = sessions.map(s => {
      const time = s.last_activity ? timeAgo(s.last_activity) : (s.created_at ? timeAgo(s.created_at) : '');
      const label = s.name;
      const isAlive = s.status === 'running' && s.alive !== false;
      const dotClass = isAlive ? 'running' : 'stopped';

      const actionBtn = isAlive
        ? `<button class="session-action session-action-stop" onclick="event.preventDefault();event.stopPropagation();stopSession('${s.name}')" title="Stop">&#9632;</button>`
        : `<button class="session-action session-action-resume" onclick="event.preventDefault();event.stopPropagation();resumeSession('${s.name}')" title="Resume">&#9654;</button>`;

      return `<a class="session-row" href="/tui?session=${encodeURIComponent(s.name)}">
        <div class="session-dot ${dotClass}"></div>
        <div class="session-body">
          <div class="session-title">${label}</div>
        </div>
        <div class="session-date">${time}</div>
        <div class="session-actions">${actionBtn}</div>
      </a>`;
    }).join('');

    return `<div class="sessions-group">
      <div class="sessions-group-header" onclick="toggleSessionGroup(this)">
        <div class="sessions-group-avatar">${sessionSprite || emoji}</div>
        <span class="sessions-group-name">${wolt}</span>
        <span class="sessions-group-meta">${metaText}</span>
        <span class="sessions-group-chevron">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 9l6 6 6-6"/></svg>
        </span>
      </div>
      <div class="sessions-group-body">
        <div class="sessions-group-inner">${rows}</div>
      </div>
    </div>`;
  }).join('');
}

// ── Session group toggle ──
function toggleSessionGroup(header) {
  header.querySelector('.sessions-group-chevron').classList.toggle('collapsed');
  header.nextElementSibling.classList.toggle('collapsed');
}

// ── Session actions ──
let runningOnly = true;

async function stopSession(name) {
  try {
    await fetch('/sessions/' + encodeURIComponent(name) + '/stop', { method: 'POST' });
    await loadSessions();
  } catch {}
}
async function resumeSession(name) {
  try {
    await fetch('/sessions/' + encodeURIComponent(name) + '/resume', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt: '' }),
    });
    await loadSessions();
  } catch {}
}
function toggleRunningOnly() {
  runningOnly = !runningOnly;
  const btn = document.getElementById('sessions-toggle-running');
  btn.classList.toggle('active', runningOnly);
  renderSessions();
}

// ── Start session ──
function startSession(woltName) {
  fetch('/sessions/new/lodge', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ wolt: woltName }),
  }).then(r => r.json()).then(data => {
    if (data.name) window.open('/tui?session=' + encodeURIComponent(data.name), '_blank');
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
      prompt: `You're working on the "${appName}" app. The viewport is showing the app at ${appName}.localhost:7777/, not your personal site.`,
    }),
  }).then(r => r.json()).then(data => {
    if (data.name) window.open('/tui?session=' + encodeURIComponent(data.name), '_blank');
  }).catch(() => {});
}

// ── Create wolt modal ──
let createSelectedType = null;

function openCreateWolt(e) {
  if (e) e.preventDefault();
  document.getElementById('create-modal').classList.add('open');
  document.getElementById('create-name').value = '';
  createSelectedType = null;
  document.querySelectorAll('.type-card').forEach(c => c.classList.remove('selected'));
  document.getElementById('create-submit').disabled = true;
  document.getElementById('create-submit').textContent = 'Create';
  document.getElementById('create-error').style.display = 'none';
  setTimeout(() => document.getElementById('create-name').focus(), 50);
}

function closeCreateWolt() {
  document.getElementById('create-modal').classList.remove('open');
}

function pickType(el) {
  document.querySelectorAll('.type-card').forEach(c => c.classList.remove('selected'));
  el.classList.add('selected');
  createSelectedType = el.dataset.type;
  updateCreatePreview();
}

function updateCreatePreview() {
  const name = document.getElementById('create-name').value.trim().toLowerCase().replace(/[^a-z0-9-]/g, '');
  const submit = document.getElementById('create-submit');
  document.getElementById('create-error').style.display = 'none';
  submit.disabled = !(name && createSelectedType);
}

async function submitCreateWolt() {
  const name = document.getElementById('create-name').value.trim().toLowerCase().replace(/[^a-z0-9-]/g, '');
  if (!name || !createSelectedType) return;

  const submit = document.getElementById('create-submit');
  const error = document.getElementById('create-error');
  submit.textContent = 'Creating...';
  submit.disabled = true;
  error.style.display = 'none';

  try {
    const res = await fetch('/sessions/new/create', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, type: createSelectedType }),
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
    if (data.name) window.open('/tui?session=' + encodeURIComponent(data.name), '_blank');
    loadWolts();
  } catch (e) {
    error.textContent = 'network error — try again';
    error.style.display = 'block';
    submit.textContent = 'Create';
    submit.disabled = false;
  }
}

// ── Onboard ──
async function openOnboard() {
  // The entrypoint already started a bare Claude session in 'main' with
  // the onboard page as viewport when no auth is detected. Just go there.
  location.href = '/tui';
}

// ── Keyboard ──
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeCreateWolt();
});

// ── Init ──
fetch('/onboard-status').then(r => r.json()).then(d => {
  if (!d.has_oauth) document.getElementById('onboard-banner').style.display = '';
}).catch(() => {});

// Replace type card emoji with pixel art sprites
document.querySelectorAll('.type-card').forEach(card => {
  const type = card.dataset.type;
  const sprite = woltSpriteAvatar(type, 40);
  if (sprite) card.querySelector('.type-card-emoji').innerHTML = sprite;
});

loadHarnesses().finally(loadWolts);
loadApps();
loadSessions();

console.log('%c🦫', 'font-size:3rem');
console.log('%cwoltspace — the lodge', 'color:#C98B2A;font-family:monospace');

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}
