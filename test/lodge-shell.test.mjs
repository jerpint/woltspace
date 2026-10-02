import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';

const source = readFileSync(new URL('../public/static/lodge-shell.js', import.meta.url), 'utf8');
const tui = readFileSync(new URL('../templates/tui.html', import.meta.url), 'utf8');

// No document in this context: the file defines its rules and the page code returns at once.
const context = { URL, URLSearchParams };
runInNewContext(source, context);
const logic = context.LodgeShellLogic;
const origin = 'https://lodge.example';

test('a saved page is judged after it is normalized, and only lodge pages come back', () => {
  assert.equal(logic.lodgePage('/', origin), '/');
  assert.equal(logic.lodgePage('/?view=apps', origin), '/?view=apps');
  assert.equal(logic.lodgePage('/settings', origin), '/settings');
  assert.equal(logic.lodgePage('/w/wolter-white', origin), '/w/wolter-white');
  assert.equal(logic.lodgePage('/w/wolter-white?tab=about', origin), '/w/wolter-white');

  for (const bad of [
    '/tui?session=stopped',
    '/x/../tui?session=stopped',
    '/%74ui?session=stopped',
    '/shell',
    '/w/../shell',
    '/w/a/b',
    '/?view=nope',
    '/app/blog/',
    '//evil.example/',
    '/\\evil.example/',
    'https://evil.example/',
    'javascript:alert(1)',
    '',
    null,
    42,
    { url: '/' },
  ]) {
    assert.equal(logic.lodgePage(bad, origin), null, String(bad));
  }
});

test('a reload brings back only tabs whose sessions are online now', () => {
  const saved = { tabs: ['a-b-c-111111', 'gone-b-c-222222', 'a-b-c-111111', 7, '../x', 'd-e-f-333333'], current: 'gone-b-c-222222' };
  const back = logic.restorableTabs(saved, ['d-e-f-333333', 'a-b-c-111111']);
  assert.deepEqual([...back.tabs], ['a-b-c-111111', 'd-e-f-333333']);
  assert.equal(back.current, null);

  const kept = logic.restorableTabs({ tabs: ['a-b-c-111111'], current: 'a-b-c-111111' }, ['a-b-c-111111']);
  assert.equal(kept.current, 'a-b-c-111111');

  for (const junk of [null, undefined, 'x', { tabs: 'nope' }, { tabs: [{}] }]) {
    const none = logic.restorableTabs(junk, ['a-b-c-111111']);
    assert.deepEqual([...none.tabs], []);
    assert.equal(none.current, null);
  }
});

test('a session that hands off takes its tab with it', () => {
  let moved = logic.redirectTabs(['a', 'b', 'c'], 'b', 'b', 'z');
  assert.deepEqual([...moved.tabs], ['a', 'z', 'c']);
  assert.equal(moved.current, 'z');

  moved = logic.redirectTabs(['a', 'b', 'z'], 'a', 'b', 'z');   // the target already has a tab
  assert.deepEqual([...moved.tabs], ['a', 'z']);
  assert.equal(moved.current, 'a');
});

test('tabs the layout opens by itself are attach-only', () => {
  // Reload, tab switch, keyboard, neighbour after a close.
  assert.match(source, /if \(current\) sessionFrame\(current, false\);   \/\/ attach only/);
  assert.match(source, /openSession\(tab\.dataset\.name, false\)/);
  assert.match(source, /openSession\(tabs\[next\], false\)/);
  assert.match(source, /\+ \(wake \? '' : '&attach=1'\)/);
  assert.match(tui, /if \(attachOnly\) attachIfRunning\(\); else ensureSessionAlive\(\);/);
  assert.match(tui, /setTimeout\(attachOnly \? attachIfRunning : connectTUI, 2000\)/);
});

// Runs the session screen's real attach-only function against a fake lodge.
// `answer` is what GET /sessions/{name} gives: { status, body } or 'throws'.
async function attachOnly(answer, { handoff = false, inFlight = false } = {}) {
  const fn = tui.match(/async function attachIfRunning\(\) \{[\s\S]*?^  \}/m)?.[0];
  assert.ok(fn, 'attachIfRunning function found');
  const calls = [];
  const context = {
    session: 'maple-quiet-brook-abc123',
    encodeURIComponent,
    fetch: async (url, options = {}) => {
      calls.push([options.method || 'GET', url]);
      if (answer === 'throws') throw new Error('network');
      return { ok: answer.status === 200, status: answer.status, json: async () => answer.body };
    },
    connectTUI: () => calls.push(['attach']),
    setStatus: (state, text) => calls.push(['status', state, text]),
    reportOffline: () => calls.push(['offline']),
    setTimeout: (callback, ms) => { calls.push(['retry', callback.name, ms]); return 1; },
    metaInFlight: Promise.resolve(inFlight),
    pollCurrent: async () => { calls.push(['settle-handoff']); return handoff; },
  };
  await runInNewContext(`${fn}; attachIfRunning()`, context);
  return calls;
}
const GET = ['GET', '/sessions/maple-quiet-brook-abc123'];
const record = body => ({ status: 200, body });

test('an attach-only tab attaches to a live window and never resumes', async () => {
  for (const body of [
    { status: 'running', agent_alive: true, tmux_alive: true },
    // What the lodge really answers when the agent exited and its window survives.
    { status: 'orphaned', agent_alive: false, tmux_alive: true },
  ]) {
    assert.deepEqual(await attachOnly(record(body)), [GET, ['attach']]);
  }
});

test('an attach-only tab says offline only on a clear answer, and never wakes the session', async () => {
  for (const answer of [
    record({ status: 'stopped', agent_alive: false, tmux_alive: false }),
    record({ status: 'orphaned', agent_alive: false, tmux_alive: false }),
    { status: 404, body: { error: 'not found' } },
  ]) {
    assert.deepEqual(await attachOnly(answer), [GET, ['settle-handoff'], ['status', 'disconnected', 'offline'], ['offline']]);
  }
});

test('a failed liveness request is not an answer: the tab asks again and keeps its place', async () => {
  for (const answer of ['throws', { status: 500, body: {} }, { status: 502, body: {} }]) {
    assert.deepEqual(await attachOnly(answer), [GET, ['status', 'disconnected', 'reconnecting'], ['retry', 'attachIfRunning', 2000]]);
  }
});

test('a pending hand-off is settled before a tab goes offline', async () => {
  const stopped = record({ status: 'stopped', agent_alive: false, tmux_alive: false });
  assert.deepEqual(await attachOnly(stopped, { handoff: true }), [GET, ['settle-handoff']]);
  assert.deepEqual(await attachOnly(stopped, { inFlight: true }), [GET]);
});

test('the lodge terminal `main` is never a tab', () => {
  assert.match(source, /!SESSION_NAME\.test\(name\) \|\| name === 'main'\) return;/);
  assert.match(source, /to === name \|\| to === 'main'\) return;/);
});

test('an error answer from the lodge lists is not an empty lodge', () => {
  assert.match(source, /if \(!r\.ok\) throw new Error/);
  assert.match(source, /!Array\.isArray\(res\[0\]\) \|\| !res\[1\] \|\| !Array\.isArray\(res\[1\]\.sessions\)\) throw/);
});

test('the layout refuses to run inside a frame', () => {
  assert.match(source, /if \(window\.top !== window\) \{ document\.body\.replaceChildren\(\); return; \}/);
});
