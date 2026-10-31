// The inbox viewer's list on the fake DOM: mail text stays text, and a reply for
// a filter the owner already left never lands under the new one.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import { Node } from './fake-dom.mjs';

const SOURCE = readFileSync(new URL('../public/static/wolt-shell/inbox-viewer.js', import.meta.url), 'utf8');
const TRICKY = '<img src=x onerror=alert(1)> & </script>\nline two';

// The viewer sets classes as an attribute; the fake DOM matches on className.
function element(tag) {
  const n = new Node(tag);
  const set = n.setAttribute.bind(n);
  n.setAttribute = (name, value) => { if (name === 'class') n.className = value; set(name, value); };
  return n;
}
function load() {
  const asked = [];
  const window = {};
  const context = {
    window, URLSearchParams,
    document: { createElement: element, body: new Node('body') },
    fetch: url => new Promise(resolve => asked.push({ url, resolve })),
  };
  vm.runInNewContext(SOURCE, context);
  return { inbox: window.__woltInbox, asked };
}
function answer(call, messages, next = null) {
  call.resolve({ ok: true, status: 200, json: async () => ({ ok: true, now: 1000, messages, next }) });
}
const message = (sender, text, extra = {}) =>
  ({ seq: 1, sender, sender_wolt: null, human: false, at: 990, state: 'queued', text, ...extra });
const tick = () => new Promise(resolve => setImmediate(resolve));
const rows = box => box.querySelectorAll('wi-row');

test('mail text and sender names are rendered as text, never as HTML', async () => {
  const { inbox, asked } = load();
  const box = new Node('div');
  inbox.page(box);
  assert.equal(asked[0].url, '/inbox/messages?');
  answer(asked[0], [message('<b>pal</b>', TRICKY, { sender_wolt: 'pal' }), message('connector:t', 'hi', { human: true })]);
  await tick();
  const [first, second] = rows(box);
  assert.equal(first.querySelector('wi-text').textContent, TRICKY);
  assert.equal(first.querySelector('wi-sender').textContent, '<b>pal</b>');
  assert.equal(first.querySelector('wi-sender').attributes.href, '/wolt/pal/site/');
  assert.equal(second.querySelector('wi-sender').tagName, 'B');  // not a known wolt: no link
});

test('switching the filter drops the answer for the wolt left behind', async () => {
  const { inbox, asked } = load();
  const box = new Node('div');
  const view = inbox.page(box);
  view.setFilter('pal');
  assert.deepEqual(asked.map(c => c.url), ['/inbox/messages?', '/inbox/messages?wolt=pal']);
  answer(asked[1], [message('nunu', 'for pal')]);
  answer(asked[0], [message('zed', 'from every wolt')]);  // the slower, older answer
  await tick();
  assert.deepEqual(rows(box).map(r => r.querySelector('wi-text').textContent), ['for pal']);
});
