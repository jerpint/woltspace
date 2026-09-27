// The wolves page. Each wolf is one sentence of pills; every change applies
// straight away through the /wolf/crons API (the one `woltspace wolf` drives).
// No cron syntax on screen: schedules are built from and read back into pills,
// and anything the pills can't express stays "on a custom schedule".

const root = document.querySelector('[data-wolves-root]');

if (root) {
  const $ = (s, r = document) => r.querySelector(s);
  const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const L = $('[data-list]', root);
  const globalState = $('[data-global-state]', root);
  const globalMessage = $('[data-global-message]', root);
  const WOLTS = JSON.parse($('#wolves-wolts').textContent || '[]');
  const EMOJI_BY_TYPE = typeof WOLT_EMOJI === 'object' ? WOLT_EMOJI : {};
  const EMOJI = Object.fromEntries(WOLTS.map((w) => [w.name, EMOJI_BY_TYPE[w.type] || '🐾']));
  const CHAN_LABEL = { telegram: 'Telegram', slack: 'Slack' };
  const DAYN = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
  let TZ = Intl.DateTimeFormat().resolvedOptions().timeZone;
  let CHANNELS = [];

  function setGlobalState(kind, message) {
    globalState.classList.remove('saving', 'saved', 'error');
    if (kind) globalState.classList.add(kind);
    globalMessage.textContent = message;
  }

  async function api(method, url, body) {
    const response = await fetch(url, {
      method,
      headers: body ? { 'Content-Type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || 'The lodge could not make that change.');
    return data;
  }
  const cronUrl = (wolt, name, tail = '') => `/wolf/crons/${encodeURIComponent(wolt)}/${encodeURIComponent(name)}${tail}`;

  // ---------- lodge time: wolves fire on the clock of the machine the lodge runs on ----------
  let pf, fmtDay, fmtTime;
  function setZone(tz) {
    try { new Intl.DateTimeFormat('en-US', { timeZone: tz }); TZ = tz; } catch { /* keep the browser's zone */ }
    pf = new Intl.DateTimeFormat('en-US', { timeZone: TZ, hourCycle: 'h23', year: 'numeric', month: 'numeric', day: 'numeric', hour: 'numeric', minute: 'numeric' });
    fmtDay = new Intl.DateTimeFormat('en-US', { timeZone: TZ, weekday: 'short', month: 'short', day: 'numeric' });
    fmtTime = new Intl.DateTimeFormat('en-US', { timeZone: TZ, hour: 'numeric', minute: '2-digit' });
  }
  setZone(TZ);
  function parts(t) { const o = {}; for (const p of pf.formatToParts(new Date(t))) o[p.type] = p.value; return { y: +o.year, mo: +o.month, d: +o.day, h: +o.hour % 24, mi: +o.minute }; }
  function offset(t) { const p = parts(t); return Date.UTC(p.y, p.mo - 1, p.d, p.h, p.mi) - Math.floor(t / 60000) * 60000; }
  function wall(y, mo, d, h, mi) { const g = Date.UTC(y, mo - 1, d, h, mi); return g - offset(g - offset(g)); }
  const localEpoch = (s) => { const m = String(s || '').match(/^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d)/); return m ? wall(+m[1], +m[2], +m[3], +m[4], +m[5]) : NaN; };
  function friendly(t) {
    const same = (a, b) => a.y === b.y && a.mo === b.mo && a.d === b.d, p = parts(t);
    const day = same(p, parts(Date.now())) ? 'today' : same(p, parts(Date.now() + 86400e3)) ? 'tomorrow' : fmtDay.format(new Date(t));
    return day + ' at ' + fmtTime.format(new Date(t));
  }
  const pad = (n) => String(n).padStart(2, '0');
  const ord = (n) => n + (n % 10 === 1 && n !== 11 ? 'st' : n % 10 === 2 && n !== 12 ? 'nd' : n % 10 === 3 && n !== 13 ? 'rd' : 'th');

  // ---------- cron entry <-> pills ----------
  function toPills(c) {
    const tom = parts(Date.now() + 86400e3), base = { time: '09:00', days: [1], dom: 1, date: `${tom.y}-${pad(tom.mo)}-${pad(tom.d)}` };
    if (c.at) { const [d, t] = c.at.split('T'); return { ...base, repeat: 'once', date: d, time: (t || '09:00').slice(0, 5) }; }
    const f = String(c.schedule || '').trim().split(/\s+/), num = (x) => /^\d+$/.test(x);
    if (f.length === 5 && num(f[0]) && num(f[1]) && +f[0] < 60 && +f[1] < 24 && f[3] === '*') {
      const [mi, h, dom, , dw] = f, time = `${pad(+h)}:${pad(+mi)}`;
      if (dom === '*' && dw === '*') return { ...base, repeat: 'daily', time };
      if (dom === '*' && dw === '1-5') return { ...base, repeat: 'weekdays', time };
      if (dom === '*' && /^[0-7](,[0-7])*$/.test(dw)) return { ...base, repeat: 'days', time, days: [...new Set(dw.split(',').map((d) => +d % 7))].sort() };
      if (num(dom) && +dom >= 1 && +dom <= 28 && dw === '*') return { ...base, repeat: 'monthly', time, dom: +dom };
    }
    return { ...base, repeat: 'custom', custom: c.schedule };
  }
  function fromPills(s) {
    const [h, m] = s.time.split(':').map(Number);
    if (s.repeat === 'once') return { at: `${s.date}T${s.time}` };
    if (s.repeat === 'custom') return { schedule: s.custom };
    if (s.repeat === 'daily') return { schedule: `${m} ${h} * * *` };
    if (s.repeat === 'weekdays') return { schedule: `${m} ${h} * * 1-5` };
    if (s.repeat === 'monthly') return { schedule: `${m} ${h} ${s.dom} * *` };
    return { schedule: `${m} ${h} * * ${[...s.days].sort().join(',')}` };
  }
  function problem(e) { // what the page can tell before asking the lodge
    const s = e.pills;
    if (!s.wolt) return 'Choose which wolt to wake up.';
    if (!e.msg.trim()) return 'Tell the wolt what to do when it wakes up.';
    if (s.repeat === 'days' && !s.days.length) return 'Keep at least one day.';
    if (s.repeat !== 'custom' && !/^\d\d:\d\d$/.test(s.time)) return 'Pick a time.';
    if (s.repeat === 'once' && !/^\d{4}-\d\d-\d\d$/.test(s.date)) return 'Pick a date.';
    return null;
  }

  // ---------- entries ----------
  let seq = 0;
  let entries = [];
  const byId = (id) => entries.find((e) => e.id === +id);
  const fromCron = (c) => ({ id: ++seq, wolt: c.wolt, c, pills: { ...toPills(c), wolt: c.wolt, notify: c.notify || '' }, msg: c.prompt || '', status: c.error || '', err: !!c.error });

  const wOpts = (sel) => (sel ? '' : '<option value="" selected>choose a wolt</option>')
    + WOLTS.map((w) => `<option value="${esc(w.name)}"${w.name === sel ? ' selected' : ''}>${esc(EMOJI[w.name])} ${esc(w.name)}</option>`).join('')
    + (sel && !WOLTS.some((w) => w.name === sel) ? `<option value="${esc(sel)}" selected>${esc(sel)}</option>` : '');
  function sentence(e) {
    const s = e.pills, opt = (v, t) => `<option value="${v}"${s.repeat === v ? ' selected' : ''}>${t}</option>`;
    let h = `<select class="wolves-pill" data-f="repeat" aria-label="how often">
      ${opt('once', 'Once')}${opt('daily', 'Every day')}${opt('weekdays', 'Every weekday')}${opt('days', 'Every week on')}${opt('monthly', 'Every month')}
      ${s.repeat === 'custom' ? opt('custom', 'On a custom schedule') : ''}</select>`;
    if (s.repeat === 'once') h += `<span>on</span><input class="wolves-pill" type="date" data-f="date" value="${esc(s.date)}" aria-label="date">`;
    if (s.repeat === 'days') h += `<span class="wolves-days">${['S', 'M', 'T', 'W', 'T', 'F', 'S'].map((d, i) => `<button type="button" data-day="${i}" aria-label="${DAYN[i]}" aria-pressed="${s.days.includes(i)}">${d}</button>`).join('')}</span>`;
    if (s.repeat === 'monthly') h += `<span>on the</span><select class="wolves-pill" data-f="dom" aria-label="day of the month">${Array.from({ length: 28 }, (_, i) => `<option value="${i + 1}"${s.dom === i + 1 ? ' selected' : ''}>${ord(i + 1)}</option>`).join('')}</select>`;
    if (s.repeat !== 'custom') h += `<span>at</span><input class="wolves-pill" type="time" data-f="time" value="${esc(s.time)}" aria-label="time">`;
    h += `<span>wake</span><select class="wolves-pill" data-f="wolt" aria-label="which wolt">${wOpts(s.wolt)}</select>`;
    return h;
  }
  function pingLine(e) { // an optional heads-up when it runs, deliberately quiet
    const s = e.pills, chans = [...new Set([...CHANNELS, ...(s.notify ? [s.notify] : [])])];
    if (!chans.length) return '';
    return `<div class="wolves-ping">🔔 Also ping me when it runs:
      <select class="wolves-mini${s.notify ? ' on' : ''}" data-f="notify" aria-label="optional ping when it runs">
      <option value=""${s.notify ? '' : ' selected'}>Off</option>${chans.map((c) => `<option value="${esc(c)}"${s.notify === c ? ' selected' : ''}>On ${esc(CHAN_LABEL[c] || c)}</option>`).join('')}</select></div>`;
  }
  const nextLine = (e) => (e.c.next_run ? 'Next: ' + friendly(Date.parse(e.c.next_run)) : e.c.at ? 'Already ran' : '');
  const TRASH = '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12M9 7V4h6v3"/></svg>';
  function cardHtml(e) {
    if (e.removed) return `<div class="wolves-card gone" data-id="${e.id}">Removed ${esc(e.wolt)}'s wolf. <button class="link" type="button" data-undo>Undo</button>
      <div class="wolves-status${e.err ? ' err' : ''}">${e.status || ''}</div></div>`;
    return `<div class="wolves-card${e.isNew ? ' new' : ''}${e.c.error ? ' error' : ''}" data-id="${e.id}">
      <div class="wolves-sentence">${sentence(e)}</div>
      <textarea class="wolves-msg" data-f="msg" rows="1" aria-label="what to tell it" placeholder="and tell it... (e.g. write my morning digest)">${esc(e.msg)}</textarea>
      ${e.isNew
        ? `${pingLine(e)}<div class="wolves-newfoot"><button class="primary" type="button" data-add>Add</button><button type="button" data-cancel>Cancel</button></div>`
        : `<div class="wolves-foot"><span class="meta">${esc(nextLine(e))}</span><span class="wolves-acts"><button class="run" type="button" data-run>▶ Run now</button><button class="icon" type="button" data-remove aria-label="Remove this wolf" title="Remove">${TRASH}</button></span></div>`}
      <div class="wolves-status${e.err ? ' err' : ''}">${e.status || ''}</div>
      ${e.isNew ? '' : pingLine(e) + runsHtml(e)}
    </div>`;
  }
  function grow(ta) { ta.style.height = 'auto'; ta.style.height = ta.scrollHeight + 'px'; }
  function paint(e) { // redraw one card in place, so nothing else on the page moves
    const old = L.querySelector(`[data-id="${e.id}"]`);
    if (!old) return;
    if (old.contains(document.activeElement)) document.activeElement.blur();
    const tmp = document.createElement('div');
    tmp.innerHTML = cardHtml(e);
    old.replaceWith(tmp.firstElementChild);
    L.querySelectorAll(`[data-id="${e.id}"] textarea`).forEach(grow);
  }
  function patch(e) { // status + next line only: a tap elsewhere is never swallowed by a redraw
    const card = L.querySelector(`[data-id="${e.id}"]`);
    if (!card) return;
    const st = $('.wolves-status', card), mt = $('.meta', card);
    st.innerHTML = e.status || ''; st.classList.toggle('err', !!e.err);
    if (mt) mt.textContent = nextLine(e);
  }
  function render() {
    const live = entries.filter((e) => !e.removed || e.status);
    if (!live.length) {
      L.innerHTML = `<div class="wolves-card wolves-empty">
        <div class="big">🐺</div>
        <p>Nothing scheduled yet.</p>
        <button class="primary" type="button" data-new>Schedule a wolf</button>
        <div class="wolves-ideas">Or start from one of these:
          <button type="button" data-idea="digest">☀️ A morning digest, every day at 8</button>
          <button type="button" data-idea="review">📋 A weekly review, Fridays at 4</button>
          <button type="button" data-idea="remind">⏰ A reminder, tomorrow at 9</button>
        </div></div>`;
      return;
    }
    L.innerHTML = live.map(cardHtml).join('')
      + (entries.some((e) => e.isNew) ? '' : '<div class="wolves-add"><button class="primary" type="button" data-new>Schedule a wolf</button></div>');
    L.querySelectorAll('textarea').forEach(grow);
  }

  // ---------- recent runs: each wolf keeps its own, collapsed until asked for ----------
  let runs = [];
  const runsFor = (e) => runs.filter((r) => r.wolt === e.wolt && r.name === e.c.name);
  function runsHtml(e) {
    const mine = runsFor(e);
    if (!mine.length) return '';
    const rows = mine.slice(0, 5).map((r) => {
      const when = r.fresh ? 'just now' : isNaN(r.t) ? 'earlier' : friendly(r.t);
      return `<div class="r${r.fresh ? ' fresh' : ''}"><span>${esc(when)}${r.manual ? ' <small>by hand</small>' : ''}</span>
        ${r.link ? `<a href="${esc(r.link)}" target="_blank" rel="noopener">open session</a>` : '<span class="wolves-quiet">no session</span>'}</div>`;
    }).join('');
    return `<details class="wolves-runs"${e.runsOpen ? ' open' : ''}><summary>Recent runs · ${mine.length}</summary>${rows}</details>`;
  }

  // ---------- apply: every change goes straight to the lodge, one at a time per wolf ----------
  function queue(e, job) {
    e.chain = (e.chain || Promise.resolve()).then(job, job);
    return e.chain;
  }
  function body(e) {
    return { ...fromPills(e.pills), prompt: e.msg.trim(), notify: e.pills.notify || '' };
  }
  function commit(e, soft) {
    const draw = soft ? patch : paint;
    const why = problem(e);
    if (why) { e.status = why; e.err = true; return draw(e); }
    return queue(e, async () => {
      const payload = body(e);
      if (e.pills.wolt !== e.wolt) payload.wolt = e.pills.wolt;
      setGlobalState('saving', 'Saving…');
      try {
        const saved = await api('PUT', cronUrl(e.wolt, e.c.name), payload);
        e.c = saved; e.wolt = saved.wolt; e.status = '✓ Saved'; e.err = false;
        setGlobalState('saved', 'Saved');
      } catch (error) {
        e.status = esc(error.message) + ' Nothing was changed.'; e.err = true;
        setGlobalState('error', 'Could not save');
      }
      draw(e);
    });
  }
  function onChange(e, f, v) {
    if (f === 'msg') e.msg = v; else if (f === 'dom') e.pills.dom = +v; else e.pills[f] = v;
    if (f === 'repeat' && v === 'days' && !e.pills.days.length) e.pills.days = [1];
    if (e.isNew) { e.status = ''; e.err = false; return f === 'msg' ? patch(e) : paint(e); }
    commit(e, f === 'msg');
  }
  function newWolf(fill) {
    const open = entries.find((e) => e.isNew);
    if (open) return L.querySelector(`[data-id="${open.id}"]`).scrollIntoView({ behavior: 'smooth', block: 'center' });
    const e = { id: ++seq, isNew: true, wolt: '', c: { name: '' }, pills: { ...toPills({}), repeat: 'daily', wolt: '', notify: '' }, msg: '' };
    if (fill) fill(e);
    entries.unshift(e); render();
    const card = L.querySelector(`[data-id="${e.id}"]`);
    card.scrollIntoView({ behavior: 'smooth', block: 'center' });
    $('[data-f=wolt]', card).focus({ preventScroll: true });
  }
  const IDEAS = {
    digest: (e) => { e.pills.repeat = 'daily'; e.pills.time = '08:00'; e.msg = "Write my morning digest: what happened in the lodge overnight, and what's next today."; },
    review: (e) => { e.pills.repeat = 'days'; e.pills.days = [5]; e.pills.time = '16:00'; e.msg = 'Write a weekly review of what we got done this week.'; },
    remind: (e) => { e.pills.repeat = 'once'; e.pills.time = '09:00'; e.msg = 'Remind me to '; },
  };

  // ---------- events ----------
  L.addEventListener('change', (ev) => {
    const t = ev.target, card = t.closest('[data-id]');
    if (!card || !t.dataset.f) return;
    onChange(byId(card.dataset.id), t.dataset.f, t.value);
  });
  L.addEventListener('toggle', (ev) => {
    const card = ev.target.closest && ev.target.closest('[data-id]');
    if (card && ev.target.matches('details.wolves-runs')) byId(card.dataset.id).runsOpen = ev.target.open;
  }, true);
  L.addEventListener('input', (ev) => { if (ev.target.matches('textarea')) grow(ev.target); });
  L.addEventListener('keydown', (ev) => { // Enter in the message = done (Shift+Enter for a new line)
    if (ev.target.matches('textarea') && ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); ev.target.blur(); }
  });
  L.addEventListener('click', async (ev) => {
    const b = ev.target.closest('button');
    if (!b) return;
    if (b.hasAttribute('data-new')) return newWolf();
    if (b.dataset.idea) return newWolf(IDEAS[b.dataset.idea]);
    const card = b.closest('[data-id]'), e = card && byId(card.dataset.id);
    if (!e) return;
    if (b.dataset.day !== undefined) {
      const d = +b.dataset.day, has = e.pills.days.includes(d);
      if (has && e.pills.days.length === 1) { e.status = 'Keep at least one day.'; e.err = true; return paint(e); }
      e.pills.days = has ? e.pills.days.filter((x) => x !== d) : [...e.pills.days, d];
      if (e.isNew) { e.status = ''; e.err = false; return paint(e); }
      return commit(e);
    }
    if (b.hasAttribute('data-run')) {
      b.disabled = true;
      return queue(e, async () => {
        try {
          const started = await api('POST', cronUrl(e.wolt, e.c.name, '/fire'));
          runs.forEach((r) => { r.fresh = false; });
          runs.unshift({ wolt: e.wolt, name: e.c.name, t: Date.now(), link: started.url, manual: true, fresh: true });
          e.runsOpen = true; e.status = ''; e.err = false;
        } catch (error) {
          e.status = esc(error.message); e.err = true;
        }
        paint(e);
      });
    }
    if (b.hasAttribute('data-remove')) {
      return queue(e, async () => {
        try {
          await api('DELETE', cronUrl(e.wolt, e.c.name));
          e.removed = true; e.status = ''; e.err = false;
          setGlobalState('saved', 'Removed');
        } catch (error) {
          e.status = esc(error.message); e.err = true;
        }
        paint(e);
      });
    }
    if (b.hasAttribute('data-undo')) {
      return queue(e, async () => {
        const c = e.c, restore = { wolt: e.wolt, name: c.name, prompt: c.prompt };
        if (c.at) restore.at = c.at; else restore.schedule = c.schedule;
        if (c.notify) restore.notify = c.notify;
        if (c.catch_up === false) restore.catch_up = false;
        try {
          e.c = await api('POST', '/wolf/crons', restore);
          e.removed = false; e.status = '✓ Back on the schedule'; e.err = false;
        } catch (error) {
          e.status = esc(error.message); e.err = true;
        }
        paint(e);
      });
    }
    if (b.hasAttribute('data-cancel')) { entries = entries.filter((x) => x !== e); return render(); }
    if (b.hasAttribute('data-add')) {
      const why = problem(e);
      if (why) { e.status = why; e.err = true; return paint(e); }
      b.disabled = true;
      return queue(e, async () => {
        const payload = { wolt: e.pills.wolt, ...body(e) };
        if (!payload.notify) delete payload.notify;
        try {
          const saved = await api('POST', '/wolf/crons', payload);
          Object.assign(e, { isNew: false, wolt: saved.wolt, c: saved, status: '✓ Scheduled', err: false });
          setGlobalState('saved', 'Saved');
          render();
        } catch (error) {
          e.status = esc(error.message); e.err = true;
          paint(e);
        }
      });
    }
  });

  // ---------- load ----------
  (async () => {
    try {
      const [data, fires] = await Promise.all([api('GET', '/wolf/crons'), api('GET', '/wolf/fires?limit=50')]);
      setZone(data.tz || TZ);
      CHANNELS = data.channels || [];
      entries = (data.crons || []).map(fromCron);
      for (const bad of data.errors || []) {
        L.insertAdjacentHTML('beforebegin', `<div class="wolves-card error"><div class="wolves-status err">${esc(bad.wolt)}'s wolf.json can't be read, so its wolves are hidden: ${esc(bad.error)}</div></div>`);
      }
      runs = (fires.fires || [])
        .filter((f) => f.event === 'dispatched' || (f.event === 'manual' && !f.error))
        .map((f) => ({ wolt: f.owner, name: f.cron, t: localEpoch(f.ts), link: f.link, manual: f.event === 'manual' }));
      render();
    } catch (error) {
      L.innerHTML = `<div class="wolves-card"><div class="wolves-status err">Could not reach the wolves: ${esc(error.message)}</div></div>`;
    }
  })();
}
