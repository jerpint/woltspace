import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { runInNewContext } from 'node:vm';

const root = new URL('../', import.meta.url).pathname;
const source = readFileSync(join(root, 'public/static/session-row.js'), 'utf8');

function filesBelow(dir) {
  return readdirSync(dir, { withFileTypes: true }).flatMap(entry => {
    const path = join(dir, entry.name);
    return entry.isDirectory() ? filesBelow(path) : [path];
  });
}

test('session row owns session stop/resume fetches and row construction', () => {
  const candidates = [
    ...filesBelow(join(root, 'public/static')).filter(path => path.endsWith('.js')),
    ...filesBelow(join(root, 'templates')).filter(path => path.endsWith('.html')),
  ];
  for (const path of candidates) {
    if (path.endsWith('/public/static/session-row.js')) continue;
    if (path.endsWith('/templates/tui.html')) continue; // terminal lifecycle is explicitly out of scope
    const text = readFileSync(path, 'utf8');
    assert.doesNotMatch(text, /\/sessions\/(?:[^\n]*)(?:\/stop|\/resume)/, path);
    assert.doesNotMatch(text, /(?:className\s*=\s*['"`][^\n]*session-row|classList\.add\(\s*['"`]session-row|(?:make|el|lodgeElement)\(\s*[^,]+,\s*['"`]session-row)/, path);
  }
  assert.match(source, /\/stop|\$\{action\}/);
  assert.match(source, /`session-row\$\{opts\.compact/);
});

class Classes {
  constructor(node) { this.node = node; }
  values() { return this.node.className.split(/\s+/).filter(Boolean); }
  toggle(name, force) {
    const set = new Set(this.values());
    const on = force === undefined ? !set.has(name) : force;
    on ? set.add(name) : set.delete(name);
    this.node.className = [...set].join(' ');
  }
}
class Node {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {}; this.className = ''; this.listeners = {}; this.classList = new Classes(this); }
  set textContent(value) { this._text = String(value); if (value === '') this.children = []; }
  get textContent() { return this._text || this.children.map(child => child.textContent).join(''); }
  append(...nodes) { nodes.forEach(node => { node.parentNode = this; this.children.push(node); }); }
  appendChild(node) { this.append(node); return node; }
  prepend(node) { node.parentNode = this; this.children.unshift(node); }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  replaceWith(node) { const i = this.parentNode.children.indexOf(this); node.parentNode = this.parentNode; this.parentNode.children[i] = node; }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(n => n !== this); }
  setAttribute(name, value) { this[name] = String(value); }
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
  dispatch(name, extra = {}) { const event = { preventDefault() {}, stopPropagation() {}, ...extra }; return Promise.all((this.listeners[name] || []).map(fn => fn(event))); }
  focus() { document.activeElement = this; }
  contains(node) { return this === node || this.children.some(child => child.contains(node)); }
  closest(selector) { return selector === '.session-row' && this.className.split(' ').includes('session-row') ? this : this.parentNode?.closest(selector); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector) {
    const cls = selector.startsWith('.') ? selector.slice(1).split('[')[0] : null;
    const hits = [];
    const visit = node => { if (cls && node.className.split(' ').includes(cls)) hits.push(node); node.children.forEach(visit); };
    this.children.forEach(visit); return hits;
  }
}
const document = { activeElement: null, createElement: tag => new Node(tag) };

function load(fetch) {
  const window = {};
  runInNewContext(source, {
    window, document, fetch, encodeURIComponent, JSON, Date,
    setTimeout: (fn, delay = 0) => { if (!delay) fn(); return 1; },
    sessionIsOnline: s => s.status === 'running' && s.alive === true,
    sessionStateText: s => s.status === 'running' && s.alive === true ? 'online' : 'offline · just now',
    timeAgo: () => 'just now',
  });
  return window;
}

test('control confirms stop, updates locally, and resumes without confirmation', async () => {
  const calls = [];
  const api = load(async (url, options) => { calls.push([url, options]); return { ok: true }; });
  const online = { name: 'n00b-one', title: 'One', status: 'running', alive: true, openable: true };
  const row = api.sessionRow(online);
  let button = row.querySelector('.session-control-button');
  assert.equal(button.textContent, '■');
  await button.dispatch('click');
  assert.equal(row.querySelector('.session-confirm-label').textContent, 'Stop?');
  await row.querySelector('.yes').dispatch('click');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls[0][0], '/sessions/n00b-one/stop');
  assert.equal(row.querySelector('.session-control-button').textContent, '▶');
  assert.equal(row.querySelector('.session-sub').textContent, 'offline · just now');
  button = row.querySelector('.session-control-button');
  await button.dispatch('click');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls[1][0], '/sessions/n00b-one/resume');
  assert.equal(calls[1][1].body, '{"prompt":""}');
  assert.equal(row.querySelector('.session-control-button').textContent, '■');
});

test('failed stop restores the stop control and reports inline', async () => {
  const api = load(async () => ({ ok: false }));
  const row = api.sessionRow({ name: 'bad', status: 'running', alive: true, openable: true });
  await row.querySelector('.session-control-button').dispatch('click');
  await row.querySelector('.yes').dispatch('click');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(row.querySelector('.session-control-button').textContent, '■');
  assert.equal(row.querySelector('.session-control-error').textContent, "couldn't stop");
});

test('poll updates preserve a control and its keyboard focus while state is unchanged', () => {
  const api = load(async () => ({ ok: true }));
  const row = api.sessionRow({ name: 'same', title: 'Before', status: 'running', alive: true });
  const control = row.querySelector('.session-control');
  const button = row.querySelector('.session-control-button');
  button.focus();
  api.updateSessionRow(row, { name: 'same', title: 'After', status: 'running', alive: true });
  assert.equal(row.querySelector('.session-control'), control);
  assert.equal(document.activeElement, button);
  assert.equal(row.querySelector('.session-title').textContent, 'After');
});
