import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';

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

// Just enough DOM for renderSessions: class selectors, one attribute form.
class Node {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.dataset = {};
    this.className = '';
    this._text = '';
    this._html = '';
  }
  set textContent(value) { this._text = String(value); this._html = ''; this.children = []; }
  get textContent() { return this._text || this.children.map(child => child.textContent).join(''); }
  set innerHTML(value) { this._html = String(value); this._text = ''; this.children = []; }
  get innerHTML() { return this._html; }
  append(...nodes) { nodes.forEach(node => { node.parentNode = this; this.children.push(node); }); }
  appendChild(node) { this.append(node); return node; }
  insertBefore(node, ref) {
    node.parentNode = this;
    const i = ref ? this.children.indexOf(ref) : -1;
    if (i < 0) this.children.push(node); else this.children.splice(i, 0, node);
  }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(n => n !== this); }
  addEventListener() {}
  matches(selector) {
    const [cls] = selector.replace(/\[.*\]$/, '').split('.').filter(Boolean);
    return this.className.split(/\s+/).includes(cls);
  }
  querySelectorAll(selector) {
    const out = [];
    const walk = node => node.children.forEach(child => {
      if (child.matches(selector)) out.push(child);
      walk(child);
    });
    walk(this);
    return out;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

function loadRenderSessions() {
  const container = new Node('div');
  const context = {
    allWolts: [],
    allSessions: [],
    sessionTotals: {},
    WOLT_EMOJI: { raccoon: '🦝', beaver: '🦫', otter: '🦦' },
    woltSpriteAvatar: (type, size) => `<sprite ${type} ${size}>`,
    woltLabel: w => (w ? w.display_name || w.name : ''),
    sessionIsOnline: () => true,
    updateSessionRow: () => {},
    syncSessionRows: () => {},
    toggleSessionGroup: () => {},
    lodgeElement: (tag, className = '', text = '') => {
      const node = new Node(tag);
      node.className = className;
      if (text) node.textContent = text;
      return node;
    },
    document: { getElementById: id => (id === 'sessions-list' ? container : null) },
  };
  const source = [
    functionSource(lodgeSource, 'paintSessionGroupHeader'),
    functionSource(lodgeSource, 'renderSessions'),
  ].join('\n');
  runInNewContext(
    `${source}\nthis.render = (wolts, sessions) => { allWolts = wolts; allSessions = sessions; renderSessions(); };`,
    context,
  );
  return { render: context.render, container };
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
