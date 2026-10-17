import test from 'node:test';
import assert from 'node:assert/strict';

import {
  buildTree, guide, isListed, sessionLabel, sessionState, shortName, summarize,
} from '../src/tree.js';

const run = (name, wolt, extra = {}) => ({ name, wolt, status: 'running', agent_alive: true, ...extra });
const rest = (name, wolt, extra = {}) => ({ name, wolt, status: 'resting', agent_alive: false, ...extra });
const done = (name, wolt) => ({ name, wolt, status: 'stopped', agent_alive: false });

const lodge = [
  run('uxwolt-sleek-lodge-02a90d', 'uxwolt', { title: 'Release 0.5.20', last_activity: 20 }),
  rest('uxwolt-swift-grove-727686', 'uxwolt', { last_activity: 10 }),
  run('softie-sly-brook-7d7e94', 'softie', { hold: { until: null, reason: 'build' } }),
  rest('astre-brisk-tail-aa2cdd', 'astre', { title: '0.6.0rc1 review' }),
  done('n00b-old-oak-111111', 'n00b'),
];

const shape = (rows) => rows.map((r) => (r.kind === 'session' ? `  ${r.session.name}` : r.kind === 'wolt' ? r.wolt : 'resting'));

test('wolts with something running come first, sorted; resting wolts fold at the bottom', () => {
  assert.deepEqual(shape(buildTree(lodge)), [
    'softie', '  softie-sly-brook-7d7e94',
    'uxwolt', '  uxwolt-sleek-lodge-02a90d', '  uxwolt-swift-grove-727686',
    'resting',
  ]);
});

test('the resting fold opens onto its wolts, and a folded wolt hides its sessions', () => {
  const rows = buildTree(lodge, { restingOpen: true, folded: new Set(['uxwolt']) });
  assert.deepEqual(shape(rows), [
    'softie', '  softie-sly-brook-7d7e94',
    'uxwolt',
    'resting', 'astre', '  astre-brisk-tail-aa2cdd',
  ]);
  const resting = rows.find((r) => r.kind === 'resting');
  assert.equal(resting.wolts, 1);
  assert.equal(resting.total, 1);
});

test('a search unfolds everything, so a match is never hidden', () => {
  const rows = buildTree(lodge, { folded: new Set(['uxwolt']), expandAll: true });
  assert.ok(shape(rows).includes('  uxwolt-swift-grove-727686'));
  assert.ok(shape(rows).includes('  astre-brisk-tail-aa2cdd'));
});

test('stopped sessions show only with all', () => {
  assert.equal(isListed(lodge[4], false), false);
  assert.ok(!shape(buildTree(lodge)).includes('  n00b-old-oak-111111'));
  assert.ok(shape(buildTree(lodge, { showAll: true, restingOpen: true })).includes('  n00b-old-oak-111111'));
});

test('a wolt row counts its running sessions out of its listed ones', () => {
  const ux = buildTree(lodge).find((r) => r.kind === 'wolt' && r.wolt === 'uxwolt');
  assert.equal(ux.running, 1);
  assert.equal(ux.total, 2);
});

test('the summary counts running, resting and held', () => {
  assert.deepEqual(summarize(lodge), { running: 2, resting: 2, held: 1 });
});

test('state and label', () => {
  assert.equal(sessionState(lodge[0]), 'running');
  assert.equal(sessionState(lodge[2]), 'held');
  assert.equal(sessionState(lodge[1]), 'resting');
  assert.equal(sessionState(lodge[4]), 'stopped');
  assert.equal(sessionLabel(lodge[0]), 'Release 0.5.20');
  assert.equal(sessionLabel(lodge[1]), 'swift-grove');
  assert.equal(shortName({ name: 'odd-name', wolt: 'other' }), 'odd-name');
});

test('guides draw like the tree command', () => {
  const rows = buildTree(lodge, { restingOpen: true });
  const lines = rows.map((r) => guide(r));
  assert.deepEqual(lines, [
    '├─ ', '│  └─ ',
    '├─ ', '│  ├─ ', '│  └─ ',
    '└─ ', '   └─ ', '      └─ ',
  ]);
});
