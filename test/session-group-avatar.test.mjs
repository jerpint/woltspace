import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import { Node } from './fake-dom.mjs';

const lodgeSource = readFileSync(
  new URL('../public/static/lodge.js', import.meta.url),
  'utf8',
);

function functionSource(source, name) {
  const start = source.indexOf(`function ${name}(`);
  if (start === -1) return '';
  const body = source.indexOf('{', start);
  let depth = 0;
  for (let i = body; i < source.length; i += 1) {
    if (source[i] === '{') depth += 1;
    if (source[i] === '}') depth -= 1;
    if (depth === 0) return source.slice(start, i + 1);
  }
  throw new Error(`${name} is incomplete`);
}

// filter and online are for the Held tests; the avatar tests take the defaults.
function loadRenderSessions({ filter = 'online', online = () => true } = {}) {
  const container = new Node('div');
  const subtitle = new Node('div');
  const context = {
    allWolts: [],
    allSessions: [],
    sessionTotals: {},
    sessionFilter: filter,
    WOLT_EMOJI: { raccoon: '🦝', beaver: '🦫', otter: '🦦' },
    woltSpriteAvatar: (type, size) => `<sprite ${type} ${size}>`,
    woltEmoji: (w, f = '🦫') => ({ raccoon: '🦝', beaver: '🦫', otter: '🦦' })[w && w.type] || (w && w.emoji) || f,
    woltLabel: w => (w ? w.display_name || w.name : ''),
    sessionIsOnline: online,
    updateSessionRow: () => {},
    // One row per session, kept across renders, as the real one does.
    syncSessionRows: (inner, sessions) => sessions.forEach(session => {
      if (inner.querySelectorAll('.session-row').some(row => row.dataset.session === session.name)) return;
      const row = new Node('a');
      row.className = 'session-row';
      row.dataset.session = session.name;
      inner.append(row);
    }),
    toggleSessionGroup: () => {},
    lodgeElement: (tag, className = '', text = '') => {
      const node = new Node(tag);
      node.className = className;
      if (text) node.textContent = text;
      return node;
    },
    document: {
      getElementById: id => ({ 'sessions-list': container, 'sessions-subtitle': subtitle })[id] || null,
      querySelectorAll: () => [],
    },
  };
  const source = ['paintSessionGroupHeader', 'renderSessions', 'filterSessions']
    .map(name => functionSource(lodgeSource, name)).join('\n');
  runInNewContext(
    `${source}\nthis.render = (wolts, sessions) => { allWolts = wolts; allSessions = sessions; renderSessions(); };`,
    context,
  );
  const rows = () => container.querySelectorAll('.session-row').map(row => row.dataset.session);
  const groups = () => container.querySelectorAll('.sessions-group').map(group => group.dataset.wolt);
  return { render: context.render, container, subtitle, rows, groups };
}

test('a session group repaints its avatar and name once the wolts arrive', () => {
  const { render, container } = loadRenderSessions();
  const sessions = [{ name: 'gnaw-1', wolt: 'uxwolt' }];

  // Sessions answer first: no wolt data yet, so the fallback shows.
  render([], sessions);
  const avatar = () => container.querySelector('.sessions-group-avatar');
  const label = () => container.querySelector('.sessions-group-name');
  assert.equal(avatar().textContent, '🦫');
  assert.equal(label().textContent, 'uxwolt');

  // Then /wolts answers: the same group must now show the raccoon and its name.
  render([{ name: 'uxwolt', type: 'raccoon', display_name: 'UX Wolt' }], sessions);
  assert.equal(container.querySelectorAll('.sessions-group').length, 1);
  assert.equal(avatar().innerHTML, '<sprite raccoon 20>');
  assert.equal(label().textContent, 'UX Wolt');
});

test('loadWolts re-renders sessions that were drawn before it finished', () => {
  const loadWolts = functionSource(lodgeSource, 'loadWolts');
  assert.match(loadWolts, /renderSessions\(\)/);
});

// The Sessions view's Online / Held filter. Held shows only online sessions a
// hold keeps up, grouped by wolt as now.
const HOLD = { until: null, reason: 'npm run dev on :3000', since: 1 };
const live = (name, wolt, hold) => ({ name, wolt, status: 'running', alive: true, ...(hold ? { hold } : {}) });
const isOnline = s => s.status === 'running' && s.alive === true;

test('the subtitle counts held sessions, and only when there are some', () => {
  const lodge = loadRenderSessions({ online: isOnline });
  lodge.render([], [live('a', 'pal', HOLD), live('b', 'pal'), live('c', 'rex', HOLD)]);
  assert.equal(lodge.subtitle.textContent, '3 online · 3 total · 2 held');
  lodge.render([], [live('a', 'pal'), live('b', 'pal')]);
  assert.equal(lodge.subtitle.textContent, '2 online · 2 total');
});

test('Held shows only held sessions, grouped by wolt', () => {
  const lodge = loadRenderSessions({ filter: 'held', online: isOnline });
  lodge.render([], [live('a', 'pal', HOLD), live('b', 'pal'), live('d', 'owl'),
    { name: 'gone', wolt: 'owl', status: 'resting', alive: false, hold: HOLD }]);
  assert.deepEqual(lodge.groups(), ['pal']);
  assert.deepEqual(lodge.rows(), ['a']);
  assert.equal(lodge.container.querySelector('.sessions-group-meta').textContent, '1 held');
});

test('Held with nothing held says so', () => {
  const lodge = loadRenderSessions({ filter: 'held', online: isOnline });
  lodge.render([], [live('a', 'pal'), live('b', 'rex')]);
  assert.deepEqual(lodge.rows(), []);
  assert.equal(lodge.container.querySelector('.empty-state-text').textContent, 'no held sessions');
});
