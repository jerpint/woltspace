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
