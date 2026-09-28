(() => {
  const root = document.getElementById('wolt-page');
  if (!root) return;
  const name = root.dataset.woltName;
  document.body.dataset.wolt = name;
  const config = JSON.parse(document.getElementById('wolt-config').textContent || '{}');
  const body = root.querySelector('[data-body]');
  let sessions = [], apps = [], crons = [], memory = {}, manifest = {}, harnesses = { default: 'claude', harnesses: [] };
  let tab = new URLSearchParams(location.search).get('tab') || 'overview';

  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; };
  const link = (text, href, cls = '') => { const a = el('a', cls, text); a.href = href; return a; };
  const fetchJSON = async (url, fallback) => { try { const r = await fetch(url); return r.ok ? await r.json() : fallback; } catch { return fallback; } };
  const time = ts => new Date(ts * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  const day = ts => { const d = new Date(ts * 1000), now = new Date(), yesterday = new Date(Date.now() - 86400e3); return d.toDateString() === now.toDateString() ? 'Today' : d.toDateString() === yesterday.toDateString() ? 'Yesterday' : d.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' }); };
  const effective = () => { const id = config.harness || harnesses.default || 'claude'; const h = (harnesses.harnesses || []).find(x => x.id === id) || {}; const models = h.models || {}; return { id, model: config.model || models[config.type] || models.raccoon || '' }; };
  const sessionState = sessionStateText;  // shared with the sidebar and the Wolts page (lodge.js)

  function renderHeader() {
    const eng = effective(), own = sessions.filter(s => s.wolt === name).sort((a,b) => (b.last_activity || b.created_at || 0) - (a.last_activity || a.created_at || 0));
    const open = own.filter(sessionIsOpen), workingCount = open.filter(sessionIsWorking).length;
    const avatar = root.querySelector('[data-avatar]');
    const sprite = woltSpriteAvatar(config.type || 'rodent', 44);
    if (sprite) avatar.innerHTML = sprite; else avatar.textContent = WOLT_EMOJI[config.type] || '🦫';
    root.querySelector('[data-name]').textContent = name;
    root.querySelector('[data-meta]').textContent = `${config.type || 'rodent'} · ${eng.id}${eng.model ? ` · ${eng.model}` : ''} · ${woltStateText({ working: workingCount > 0, workingCount, open })}`;
    root.querySelector('[data-role]').textContent = (manifest.wolt || {}).description || config.description || (manifest.wolt || {}).role || config.role || '';
    const resume = root.querySelector('[data-resume]');
    if (own[0]) { resume.hidden = false; resume.href = `/tui?session=${encodeURIComponent(own[0].name)}`; resume.title = own[0].title || own[0].prompt_preview || own[0].name; }
    root.querySelector('[data-new]').onclick = () => startSession(name);
  }

  function panel(title) { const p = el('section', 'wolt-card-panel'); p.appendChild(el('div', 'wolt-colhead', title)); return p; }
  function kv(left, right, href = '') { const row = href ? link('', href, 'wolt-kv') : el('div', 'wolt-kv'); row.append(el('span', '', left), el('b', '', right)); return row; }

  function renderOverview() {
    const wrap = el('div', 'wolt-overview'), main = el('section', 'wolt-main'), side = el('aside', 'wolt-side');
    const own = sessions.filter(s => s.wolt === name).sort((a,b) => (b.last_activity || b.created_at || 0) - (a.last_activity || a.created_at || 0));
    main.appendChild(el('div', 'wolt-colhead', `Sessions · ${own.length}`));
    let currentDay = '', list;
    own.slice(0,60).forEach(s => {
      const stamp = s.last_activity || s.created_at || 0, group = day(stamp);
      if (group !== currentDay) { currentDay = group; main.appendChild(el('div', 'wolt-day', group)); list = el('div', 'wolt-session-list'); main.appendChild(list); }
      const row = link('', `/tui?session=${encodeURIComponent(s.name)}`, 'session-row');
      const state = sessionState(s);
      const dot = el('div', `session-dot ${sessionIsOpen(s) ? 'running' : 'stopped'}`);
      const text = el('div', 'session-body');
      const title = el('div', `session-title${s.title ? '' : ' wolt-untitled'}`, s.title || s.prompt_preview || s.prompt || 'untitled session');
      const summary = el('div', 'wolt-session-summary', `${s.summary ? `${s.summary} · ` : ''}${state}`);
      text.append(title, summary); row.append(dot, text, el('div', 'session-date', time(stamp))); list.appendChild(row);
    });
    if (!own.length) main.appendChild(el('div', 'wolt-quiet', 'No sessions yet — say hi.'));
    const wolf = panel(`Wolves · ${crons.filter(c => c.wolt === name).length}`);
    const wc = crons.filter(c => c.wolt === name); wc.forEach(c => wolf.appendChild(kv(`🐺 ${c.prompt || c.name || 'scheduled wake-up'}`, c.next_run ? new Date(c.next_run).toLocaleString([], {weekday:'short',hour:'numeric',minute:'2-digit'}) : 'scheduled'))); wolf.appendChild(link(wc.length ? 'Manage ›' : 'Schedule one ›', '/wolves', 'wolt-link'));
    const app = panel(`Apps · ${apps.filter(a => a.keeper === name).length}`); const kept = apps.filter(a => a.keeper === name); kept.forEach(a => app.appendChild(kv(`${a.emoji || '🪓'} ${a.name}`, a.running ? 'running' : 'stopped', a.url || `/?view=apps`))); if (!kept.length) app.appendChild(el('div','wolt-quiet','No apps yet.'));
    const eng = effective(), settings = panel('Settings'); settings.append(kv('Engine', eng.id), kv('Model', eng.model || 'tier default')); const change = el('button','wolt-link','Change ›'); change.onclick = () => switchTab('settings'); settings.appendChild(change);
    side.append(wolf, app, settings); wrap.append(main, side); body.replaceChildren(wrap);
  }

  function memorySection(title, text, note = '') { const d = el('details','wolt-memory'); const s = el('summary','', title + (note ? ` · ${note}` : '')); const c = el('div','wolt-memory-content', text || '_empty_'); d.append(s,c); return d; }
  function renderAbout() { const box = el('div','wolt-about'); const description = (manifest.wolt || {}).description || config.description; if (description) box.appendChild(el('p','wolt-lede',description)); box.append(memorySection('Identity',(memory.identity || '').replace(/^# .*\n+/,'')), el('div','wolt-colhead','Memory'), memorySection('Context',memory.context, memory.context_lines ? `${memory.context_lines} lines` : ''), memorySection('Learnings',memory.learnings,memory.learnings_lines ? `${memory.learnings_lines} lines` : ''), memorySection('Archive',(memory.archive || []).join('\n'),`${(memory.archive || []).length} files`)); body.replaceChildren(box); }
  async function saveSettings(patch, status) {
    status.textContent = 'Saving…';
    try {
      const response = await fetch(`/wolts/${encodeURIComponent(name)}/settings`, {
        method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(patch),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || 'Could not save settings.');
      config.harness = data.configured.harness || '';
      config.model = data.configured.model || '';
      renderHeader();
      renderSettings('Saved · applies from the next session');
    } catch (error) {
      status.textContent = error.message;
      status.classList.add('error');
    }
  }
  function choiceGroup(label, choices, selected, onChoose) {
    const section = el('section','wolt-choice-section'), title = el('div','wolt-colhead',label), pills = el('div','wolt-choices');
    choices.forEach(choice => { const button = el('button',`wolt-choice${choice.id === selected ? ' active' : ''}`,choice.label); button.type='button'; button.onclick=()=>onChoose(choice.id); pills.appendChild(button); });
    section.append(title,pills); return section;
  }
  function renderSettings(message = 'Changes apply from the next session.') {
    const box = el('div','wolt-card-panel'), eng = effective(), status = el('p','wolt-settings-status',message);
    const engines = (harnesses.harnesses || []).map(h => ({id:h.id,label:`${h.emoji || ''} ${h.label}`.trim()}));
    box.appendChild(choiceGroup('Engine',engines,eng.id,id=>saveSettings({harness:id},status)));
    const selected = (harnesses.harnesses || []).find(h=>h.id===eng.id) || {};
    const models = (selected.catalog || []).map(model=>({id:model.id,label:model.label || model.id}));
    box.appendChild(choiceGroup('Model',models,eng.model,id=>saveSettings({model:id},status)));
    if (selected.freeform_model) {
      const freeform = el('form','wolt-model-form'), input = el('input','wolt-model-input');
      input.type='text'; input.placeholder='provider/model'; input.value=eng.model || '';
      input.setAttribute('aria-label','Custom provider/model');
      const save = el('button','wolt-choice','Save model'); save.type='submit';
      freeform.onsubmit=event=>{event.preventDefault();const model=input.value.trim();if(model)saveSettings({model},status);};
      freeform.append(input,save); box.appendChild(freeform);
    }
    const creature=el('div','wolt-setting');creature.append(el('b','','Creature'),el('span','',`${config.type || 'rodent'} · permanent`));box.appendChild(creature,status);body.replaceChildren(box);
  }
  function renderSite() { const wrap=el('div');const bar=el('div','wolt-sitebar');bar.append(el('span','',`${name}'s site`),link('⤢ Expand',`/wolt/${encodeURIComponent(name)}/site/`,'btn btn-ghost'));const frame=el('iframe','wolt-site');frame.src=`/wolt/${encodeURIComponent(name)}/site/`;frame.title=`${name}'s site`;wrap.append(bar,frame);body.replaceChildren(wrap); }
  function switchTab(next) { tab = ['overview','about','settings','site'].includes(next) ? next : 'overview'; root.querySelectorAll('[data-tab]').forEach(b => b.classList.toggle('active',b.dataset.tab===tab)); history.replaceState(null,'',`/w/${encodeURIComponent(name)}${tab==='overview'?'':`?tab=${tab}`}`); ({overview:renderOverview,about:renderAbout,settings:renderSettings,site:renderSite}[tab])(); }
  root.querySelectorAll('[data-tab]').forEach(b => b.onclick=()=>switchTab(b.dataset.tab));

  Promise.all([fetchJSON('/sessions',[]),fetchJSON('/apps',[]),fetchJSON('/wolf/crons',{crons:[]}),fetchJSON(`/wolt/${encodeURIComponent(name)}/_/memory.json`,{}),fetchJSON(`/wolt/${encodeURIComponent(name)}/_/manifest.json`,{}),fetchJSON('/harnesses',{default:'claude',harnesses:[]})]).then(values => { [sessions,apps] = values; crons = values[2].crons || []; memory=values[3];manifest=values[4];harnesses=values[5]; allSessions=sessions; renderHeader();renderSidebarWolts();switchTab(tab); });
})();
