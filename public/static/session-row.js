// Shared session row and start/stop control. Loaded before lodge.js; helpers are
// resolved when the functions run, after lodge.js has defined them.
(() => {
  const make = (tag, className = '', text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  const titleFor = session => (session.title || session.name || 'untitled session').trim();
  const stampFor = session => session.last_activity || session.created_at || 0;
  const stopEvents = event => { event.preventDefault(); event.stopPropagation(); };
  const controlState = session => sessionIsOnline(session) ? 'stop' : session.openable === true ? 'resume' : 'none';

  // A hold keeps a running session up for a background task: "📌 forever" or
  // "📌 until 17:20" (another day: "Oct 18 17:20"), in the lodge's local time.
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const pad = n => String(n).padStart(2, '0');
  const clock = epoch => {
    const when = new Date(epoch * 1000);
    const time = `${pad(when.getHours())}:${pad(when.getMinutes())}`;
    return when.toDateString() === new Date().toDateString()
      ? time : `${MONTHS[when.getMonth()]} ${when.getDate()} ${time}`;
  };
  const holdLabel = hold => `📌 ${hold.until == null ? 'forever' : `until ${clock(hold.until)}`}`;
  const holdKey = hold => `${hold.until}|${hold.reason}`;

  function holdControl(session) {
    const host = make('span', 'session-hold');
    host.addEventListener('click', stopEvents);

    // Releasing kills nothing: the session rests at the next sweep only if
    // nobody is using it.
    const release = async () => {
      host.dataset.mode = 'pending';
      host.replaceChildren(make('span', 'session-hold-badge', '…'));
      try {
        const response = await fetch(`/sessions/${encodeURIComponent(host._session.name)}/hold`, { method: 'DELETE' });
        if (!response.ok) throw new Error('release');
        delete host._session.hold;
        host.remove();
      } catch {
        delete host.dataset.mode;
        render();
        const note = make('span', 'session-control-error', "couldn't release");
        host.appendChild(note);
        setTimeout(() => note.remove(), 2400);
      }
    };

    const render = () => {
      host.replaceChildren();
      const hold = host._session.hold;
      if (host.dataset.mode === 'confirm') {
        const label = make('span', 'session-confirm-label', 'Release?');
        const yes = make('button', 'session-confirm yes', 'yes');
        const no = make('button', 'session-confirm no', 'no');
        yes.type = no.type = 'button';
        yes.addEventListener('click', event => { stopEvents(event); release(); });
        no.addEventListener('click', event => { stopEvents(event); delete host.dataset.mode; render(); });
        host.append(label, yes, no);
        return;
      }
      // The reason is inline, since a phone never shows a tooltip; CSS cuts a
      // long one with an ellipsis, and the title keeps it whole.
      const badge = make('span', 'session-hold-badge');
      badge.append(make('span', 'session-hold-when', holdLabel(hold)));
      // A no-break space: a plain one at the start of a flex item collapses.
      if (hold.reason) badge.append(make('span', 'session-hold-reason', `\u00a0· ${hold.reason}`));
      badge.title = hold.reason || '';
      const button = make('button', 'session-hold-release', '🔓');
      button.type = 'button';
      button.title = 'Release the hold';
      button.setAttribute('aria-label', `Release the hold on ${titleFor(host._session)}`);
      button.addEventListener('click', event => { stopEvents(event); host.dataset.mode = 'confirm'; render(); });
      host.append(badge, button);
    };

    host._session = session;
    host._key = holdKey(session.hold);
    render();
    return host;
  }

  function updateHold(row, session, online) {
    const current = row.querySelector('.session-hold');
    if (current?.dataset.mode) return;
    if (!online || !session.hold) { current?.remove(); return; }
    if (current?._key === holdKey(session.hold)) { current._session = session; return; }
    const next = holdControl(session);
    if (current) current.replaceWith(next); else row.querySelector('.session-body').appendChild(next);
  }

  function sessionControl(session) {
    const host = make('div', 'session-control');
    host.addEventListener('click', stopEvents);
    host.addEventListener('keydown', event => {
      if (event.key === 'Escape' && host.dataset.mode === 'confirm') {
        stopEvents(event);
        delete host.dataset.mode;
        render();
      }
    });

    const renderFailure = message => {
      render();
      const note = make('span', 'session-control-error', message);
      host.appendChild(note);
      setTimeout(() => note.remove(), 2400);
    };

    const request = async action => {
      host.dataset.mode = 'pending';
      const previous = { ...session };
      render();
      try {
        const options = { method: 'POST' };
        if (action === 'resume') {
          options.headers = { 'Content-Type': 'application/json' };
          options.body = JSON.stringify({ prompt: '' });
        }
        const response = await fetch(`/sessions/${encodeURIComponent(session.name)}/${action}`, options);
        if (!response.ok) throw new Error(action);
        if (action === 'stop') {
          session.status = 'stopped'; session.alive = false;
          session.last_activity = Math.floor(Date.now() / 1000);
        } else {
          session.status = 'running'; session.alive = true;
        }
        delete host.dataset.mode;
        updateSessionRow(host.closest('.session-row'), session);
      } catch {
        Object.assign(session, previous);
        delete host.dataset.mode;
        renderFailure(action === 'stop' ? "couldn't stop" : "couldn't start");
      }
    };

    const render = () => {
      host.replaceChildren();
      if (host.dataset.mode === 'pending') {
        const pending = make('button', 'session-control-button pending', '…');
        pending.type = 'button'; pending.disabled = true;
        pending.setAttribute('aria-label', `Working on ${titleFor(session)}`);
        host.appendChild(pending);
        return;
      }
      if (host.dataset.mode === 'confirm') {
        const label = make('span', 'session-confirm-label', 'Stop?');
        const yes = make('button', 'session-confirm yes', 'yes');
        const no = make('button', 'session-confirm no', 'no');
        yes.type = no.type = 'button';
        yes.addEventListener('click', event => { stopEvents(event); request('stop'); });
        no.addEventListener('click', event => { stopEvents(event); delete host.dataset.mode; render(); });
        host.append(label, yes, no);
        host.addEventListener('focusout', () => setTimeout(() => {
          if (host.dataset.mode === 'confirm' && !host.contains(document.activeElement)) {
            delete host.dataset.mode; render();
          }
        }), { once: true });
        yes.focus();
        return;
      }
      const online = sessionIsOnline(session);
      if (!online && session.openable !== true) return;
      const action = online ? 'stop' : 'resume';
      const button = make('button', `session-control-button ${action}`, online ? '■' : '▶');
      button.type = 'button';
      button.setAttribute('aria-label', `${online ? 'Stop' : 'Start'} ${titleFor(session)}`);
      button.addEventListener('click', event => {
        stopEvents(event);
        if (online) { host.dataset.mode = 'confirm'; render(); }
        else request('resume');
      });
      host.appendChild(button);
    };

    host._session = session;
    host._state = controlState(session);
    host._render = render;
    render();
    return host;
  }

  function updateSessionRow(row, session) {
    if (!row) return;
    const control = row.querySelector('.session-control');
    if (control?.dataset.mode) return;
    const formatter = row._sessionTimeFormatter || timeAgo;
    const online = sessionIsOnline(session);
    const title = titleFor(session);
    const state = sessionStateText(session);
    row.dataset.session = session.name || '';
    row.classList.toggle('offline', !online);
    row.querySelector('.session-dot').className = `session-dot ${online ? 'running' : 'stopped'}`;
    const titleNode = row.querySelector('.session-title');
    titleNode.textContent = title;
    titleNode.classList.toggle('untitled', !session.title);
    row.querySelector('.session-sub').textContent = `${session.summary ? `${session.summary} · ` : ''}${state}`;
    row.querySelector('.session-date').textContent = stampFor(session) ? formatter(stampFor(session)) : '';
    updateHold(row, session, online);
    const nextState = controlState(session);
    if (control?._state === nextState) {
      Object.assign(control._session, session);
    } else {
      const nextControl = sessionControl(session);
      if (control) control.replaceWith(nextControl); else row.appendChild(nextControl);
    }
    row._session = session;
  }

  function sessionRow(session, opts = {}) {
    const openable = sessionIsOnline(session) || session.openable === true;
    const row = make(openable ? 'a' : 'div', `session-row${opts.compact ? ' compact' : ''}`);
    if (openable) row.href = `/tui?session=${encodeURIComponent(session.name)}`;
    else row.title = 'No conversation to reopen';
    row._sessionTimeFormatter = opts.timeFormatter || timeAgo;
    row.append(
      make('span', 'session-dot'),
      make('div', 'session-body'),
      make('span', 'session-date'),
      make('div', 'session-control'),
    );
    row.querySelector('.session-body').append(make('div', 'session-title'), make('div', 'session-sub'));
    updateSessionRow(row, session);
    return row;
  }

  function syncSessionRows(container, sessions, opts = {}) {
    const existing = new Map([...container.querySelectorAll('.session-row[data-session]')]
      .map(row => [row.dataset.session, row]));
    const added = [];
    sessions.forEach(session => {
      const row = existing.get(session.name);
      if (row) updateSessionRow(row, session);
      else added.push(sessionRow(session, opts));
    });
    added.reverse().forEach(row => container.prepend(row));
  }

  Object.assign(window, { sessionRow, sessionControl, updateSessionRow, syncSessionRows });
})();
