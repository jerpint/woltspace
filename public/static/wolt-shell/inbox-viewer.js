/* The lodge inbox viewer: the owner's read-only look at mail still waiting.
 *
 * One viewer, two mounts: the shell's pill and drawer on every wolt site, on
 * the right (shell.js loads this file; the filter starts on the site's wolt),
 * and the lodge's Inbox page (the filter starts on all wolts). It only reads
 * GET /inbox/summary and GET /inbox/messages: no fetch, ack, release or wake.
 * Mail text only ever goes in as textContent.
 */
(function () {
  if (window.__woltInbox) return;
  var me = document.currentScript && document.currentScript.src;
  var VER = me && me.indexOf('?') > 0 ? me.slice(me.indexOf('?')) : '';

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
  function get(url) {
    return fetch(url, { cache: 'no-store' }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) {
        if (r.status === 404 && d.error === 'inbox_disabled') return null;
        if (!r.ok) throw new Error(d.error || 'HTTP ' + r.status);
        return d;
      });
    });
  }

  // The list: filter, Load more, Refresh. wolt: the starting filter ('' = all wolts).
  function viewer(box, wolt) {
    var filter = wolt, names = [], next = null, gen = 0;
    var select = el('select', { class: 'ds-select', 'aria-label': 'Whose inbox',
      onchange: function () { filter = select.value; load(false); } });
    var status = el('p', { class: 'wi-status', role: 'status' });
    var list = el('ol', { class: 'wi-list' });
    var more = el('button', { type: 'button', class: 'wi-button', text: 'Load more', onclick: function () { load(true); } });
    box.append(el('div', { class: 'wi-bar' }, [select,
      el('button', { type: 'button', class: 'wi-button', text: 'Refresh', onclick: function () { load(false); } })]),
    status, list, more);

    function options() {
      var all = filter && names.indexOf(filter) < 0 ? names.concat([filter]) : names;
      select.replaceChildren(el('option', { value: '', text: 'All wolts' }));
      all.forEach(function (n) { select.append(el('option', { value: n, text: n })); });
      select.value = filter;
    }
    function row(m) {
      var who = m.sender_wolt
        ? el('a', { class: 'wi-sender', href: '/wolt/' + encodeURIComponent(m.sender_wolt) + '/site/', text: m.sender })
        : el('b', { class: 'wi-sender', text: m.sender });
      var at = new Date(m.at * 1000);
      return el('li', { class: 'wi-row' }, [
        el('div', { class: 'wi-meta' }, [who,
          el('span', { text: (m.human ? 'human' : 'agent') + ' · ' + (m.state === 'claimed' ? 'being read' : m.state) }),
          el('time', { datetime: at.toISOString(), text: at.toLocaleString() })]),
        el('pre', { class: 'wi-text', text: m.text })]);
    }
    function load(append) {
      var mine = ++gen, q = new URLSearchParams();  // a later load wins; an older answer is dropped
      if (filter) q.set('wolt', filter);
      if (append) q.set('before', next);
      else { list.replaceChildren(); next = null; }
      more.hidden = true;
      status.textContent = 'Loading…';
      get('/inbox/messages?' + q).then(function (d) {
        if (mine !== gen) return;
        if (d === null) { status.textContent = 'The inbox is off in this lodge.'; return; }
        d.messages.forEach(function (m) { list.append(row(m)); });
        next = d.next;
        more.hidden = next === null;
        status.textContent = list.children.length ? '' : 'Nothing waiting.';
      }).catch(function (e) {
        if (mine === gen) status.textContent = 'Could not read the messages (' + e.message + ').';
      });
    }
    options();
    return {
      load: load,
      stop: function () { gen++; },
      setWolts: function (list) {  // a poll that changes nothing leaves an open select alone
        if (list.join('\n') !== names.join('\n')) { names = list; options(); }
      },
      setFilter: function (n) { filter = n; options(); load(false); }
    };
  }

  // The site mount: shell.css's own pill, drawer and scrim, on the right.
  function drawer(opts) {
    var host = el('div', { id: 'wolt-inbox-host' });
    host.hidden = true;  // until the first count: an inbox that is off shows nothing
    document.body.appendChild(host);
    var root = host.attachShadow({ mode: 'open' });
    // all:initial would undo [hidden]: keep hidden meaning hidden, from the first frame.
    root.append(el('style', { text: (opts.shellVars || ':host{all:initial}') + ':host([hidden]){display:none}' }),
      el('link', { rel: 'stylesheet', href: '/static/design-system.css' + VER }),
      el('link', { rel: 'stylesheet', href: '/static/wolt-shell/shell.css' + VER }),
      el('link', { rel: 'stylesheet', href: '/static/wolt-shell/inbox-viewer.css' + VER }));
    var badge = el('span', { class: 'wi-badge', text: '…' });
    var pill = el('button', { type: 'button', class: 'pill wi-right', 'aria-label': 'Open the lodge inbox', onclick: toggle },
      [el('span', { class: 'emoji', text: '📬' }), el('span', { class: 'name', text: 'Inbox' }), badge]);
    var totals = el('p', { class: 'role' });
    var body = el('div', {});
    root.append(el('div', { class: 'scrim', onclick: toggle }),
      el('nav', { class: 'drawer wi-right', 'aria-label': 'Lodge inbox' }, [
        el('div', { class: 'title', text: 'Inbox' }), totals, body,
        el('p', { class: 'role', text: 'Viewing leaves mail untouched. Held: its reader is gone. Acknowledged text is not kept.' })]),
      pill);
    var v = viewer(body, opts.wolt || '');

    function toggle() {
      host.toggleAttribute('data-open');
      if (host.hasAttribute('data-open')) v.load(false); else { v.stop(); pill.focus(); }
    }
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && host.hasAttribute('data-open')) toggle();
    });
    function count() {
      if (document.visibilityState !== 'visible') return;
      get('/inbox/summary').then(function (d) {
        host.hidden = d === null;
        if (d === null) return;
        var s = { human: 0, agent: 0, claimed: 0, held: 0 };
        d.wolts.forEach(function (w) {
          s.human += w.queued_human; s.agent += w.queued_agent; s.claimed += w.claimed; s.held += w.held;
        });
        badge.textContent = String(s.human + s.agent);
        totals.textContent = 'Whole lodge: ' + (s.human + s.agent) + ' queued (' + s.human + ' human, ' + s.agent +
          ' agent) · ' + s.claimed + ' being read · ' + s.held + ' held';
        v.setWolts(d.wolts.map(function (w) { return w.wolt; }));
      }).catch(function () { host.hidden = false; badge.textContent = '!'; totals.textContent = 'Could not read the counts.'; });
    }
    count();
    setInterval(count, 15000);
  }

  // The lodge Inbox page mount: all wolts first; the page's cards set the filter.
  function page(box) {
    var v = viewer(box, '');
    v.load(false);
    return v;
  }

  window.__woltInbox = { drawer: drawer, page: page };
})();
