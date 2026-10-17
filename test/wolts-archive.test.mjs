import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import { Node } from './fake-dom.mjs';

const source = readFileSync(new URL('../public/static/wolts-page.js', import.meta.url), 'utf8');
const PAL = { name: 'pal', type: 'beaver', display_name: 'Pal The Builder' };
const settle = () => new Promise(resolve => setImmediate(resolve));
const click = node => node.onclick({ stopPropagation() {} });

// The Wolts page with the lodge globals it reads stubbed, and fetch answering
// from `answers` ("METHOD url" -> [status, body]).
async function loadPage({ wolts = [PAL], archived = [], answers = {} } = {}) {
  const view = new Node('div');
  view.className = 'active';
  const body = new Node('body');
  const calls = [], confirms = [], alerts = [];
  const context = {
    allWolts: wolts, WOLT_TYPES: new Set(['beaver']), RODENT_TYPES: new Set(['beaver']), WOLT_EMOJI: {},
    woltSessionSummary: w => ({ w, name: w.name, sessions: [], online: [], last: 0, total: 0 }),
    woltLabel: w => w.display_name || w.name, woltSpriteAvatar: () => '', woltStateText: () => 'offline',
    startSession() {}, openCreateWolt() {}, sessionRow: () => new Node('a'), showView() {}, scrollTo() {},
    loadWolts: async () => { calls.push('loadWolts'); },
    confirm: question => { confirms.push(question); return true; },
    alert: message => alerts.push(message),
    fetch: async (url, options = {}) => {
      const key = `${options.method || 'GET'} ${url}`;
      calls.push(key);
      const [status, answer] = answers[key] || (url === '/wolts/archived' ? [200, archived] : [200, {}]);
      return { ok: status < 300, status, json: async () => answer };
    },
    setTimeout() {}, addEventListener() {}, innerHeight: 800,
    document: {
      body, activeElement: null, addEventListener() {},
      getElementById: id => (id === 'wolts-view' ? view : null),
      createElement: tag => new Node(tag),
      createTextNode: text => { const node = new Node('#text'); node.textContent = text; return node; },
      querySelector: selector => body.querySelector(selector),
    },
  };
  context.window = context;
  runInNewContext(source, context);
  context.window.showView('wolts');
  await settle();
  const menuItems = () => {
    click(view.querySelector('.wolts-more'));
    return body.querySelector('.wolts-menu').children.filter(n => n.tagName !== 'HR');
  };
  return { view, calls, confirms, alerts, menuItems };
}

test('the wolt menu has Archive and no Rename or Delete', async () => {
  const items = (await loadPage()).menuItems();
  assert.deepEqual(items.map(n => n.textContent), ['Open page', '⚙ Settings', 'About & memory', 'Archive']);
  const archive = items.at(-1);
  assert.equal(archive.tagName, 'BUTTON');
  assert.equal(archive.disabled, undefined);
  assert.equal(archive.getAttribute('role'), 'menuitem');
});

test('Archive confirms with the exact copy, then posts and redraws', async () => {
  const page = await loadPage();
  page.calls.length = 0;
  await click(page.menuItems().at(-1));
  await settle();
  assert.deepEqual(page.confirms,
    ['Archive Pal The Builder? Its sessions stop and it leaves the lodge until you unarchive it.']);
  assert.deepEqual(page.calls, ['POST /wolts/pal/archive', 'loadWolts', 'GET /wolts/archived']);
  assert.deepEqual(page.alerts, []);
});

test('a 409 hold shows the session and its reason', async () => {
  const page = await loadPage({ answers: { 'POST /wolts/pal/archive': [409, {
    error: 'pal has a session that holds', held: [{ session: 'pal-maple-a1b2c3', reason: 'npm run dev' }] }] } });
  await click(page.menuItems().at(-1));
  await settle();
  assert.deepEqual(page.alerts, ['pal has a session that holds\npal-maple-a1b2c3: npm run dev']);
});

test('Archived section lists archived wolts with Unarchive and Delete', async () => {
  const page = await loadPage({ wolts: [], archived: [PAL] });
  const section = page.view.querySelector('.wolts-archived');
  assert.equal(section.tagName, 'DETAILS');
  assert.equal(section.open, false);
  assert.equal(section.querySelector('.wolts-group').textContent, 'Archived · 1');
  assert.equal(page.view.querySelector('.wolts-quiet'), null);  // the archived row is what shows
  const row = section.querySelector('.wolts-archived-row');
  assert.equal(row.querySelector('.wolts-name').textContent, 'Pal The Builder');
  const [unarchive, remove] = row.querySelectorAll('.btn');
  assert.deepEqual([unarchive.textContent, remove.textContent], ['Unarchive', 'Delete']);

  page.calls.length = 0;
  await click(remove);
  await click(unarchive);
  await settle();
  assert.deepEqual(page.confirms, ['Delete Pal The Builder? This wolt will not be recoverable.']);
  assert.deepEqual(page.calls.filter(c => !c.startsWith('GET') && c !== 'loadWolts'),
    ['DELETE /wolts/archived/pal', 'POST /wolts/archived/pal/unarchive']);
});

test('no Archived section when nothing is archived', async () => {
  const page = await loadPage();
  assert.equal(page.view.querySelector('.wolts-archived'), null);
  assert.ok(page.calls.includes('GET /wolts/archived'));
});
