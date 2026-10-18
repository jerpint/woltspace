import test from 'node:test';
import assert from 'node:assert/strict';
import { chmodSync, mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';

import {
  AGENT_PROMPT, checkLodge, lodgeAnswers, onPath, setupMessage, uvWoltspace,
} from '../src/setup-check.js';

const BASE = 'http://127.0.0.1:7799';
const refused = async () => { throw new TypeError('fetch failed'); };

function binDir(...names) {
  const dir = mkdtempSync(join(tmpdir(), 'setup-check-'));
  for (const name of names) {
    writeFileSync(join(dir, name), '#!/bin/sh\n');
    chmodSync(join(dir, name), 0o755);
  }
  return dir;
}

test('any HTTP answer counts as a lodge, even an error status', async () => {
  assert.equal(await lodgeAnswers(BASE, async () => ({ ok: false, status: 500 })), true);
});

test('a refused connection is tried twice, then reported', async () => {
  let calls = 0;
  assert.equal(await lodgeAnswers(BASE, async () => { calls += 1; throw new TypeError('fetch failed'); }), false);
  assert.equal(calls, 2);
});

test('PATH lookup finds executables only', () => {
  const dir = binDir('tmux');
  writeFileSync(join(dir, 'uv'), 'not executable');
  assert.equal(onPath('tmux', dir), true);
  assert.equal(onPath('uv', dir), false);
  assert.equal(onPath('codex', dir), false);
});

test('the uv-installed CLI is found by its absolute path, never on PATH', () => {
  const dir = binDir('woltspace');
  const run = (cmd, args) => {
    assert.deepEqual([cmd, ...args], ['uv', 'tool', 'dir', '--bin']);
    return { status: 0, stdout: `${dir}\n` };
  };
  assert.equal(uvWoltspace(run), join(dir, 'woltspace'));
  assert.equal(uvWoltspace(() => ({ status: 0, stdout: `${binDir()}\n` })), null);
  assert.equal(uvWoltspace(() => ({ status: 2, stdout: '' })), null);
  assert.equal(uvWoltspace(() => { throw new Error('ENOENT'); }), null);
});

test('nothing installed: the checklist points at the agent prompt and the skill', () => {
  const text = setupMessage({ base: BASE, has: { uv: false, tmux: true, codex: true }, cli: null });
  assert.match(text, /No lodge answers at http:\/\/127\.0\.0\.1:7799/);
  assert.match(text, /✗ uv {3}✓ tmux {3}✓ codex {3}✗ woltspace/);
  assert.ok(text.includes(AGENT_PROMPT));
  assert.match(text, /npx skills add jerpint\/woltspace/);
  assert.doesNotMatch(text, /uv tool install/, 'the install steps live in the README only');
});

test('no agent CLI found: the checklist names all three', () => {
  const text = setupMessage({ base: BASE, has: { uv: true, tmux: true }, cli: null });
  assert.match(text, /✗ claude \/ codex \/ opencode/);
});

test('installed but stopped: only the start command, with its full path', () => {
  const text = setupMessage({ base: BASE, has: {}, cli: '/home/me/.local/bin/woltspace' });
  assert.match(text, /\/home\/me\/\.local\/bin\/woltspace start/);
  assert.doesNotMatch(text, /npx skills/);
});

test('checkLodge prints nothing and passes when a lodge answers', async () => {
  let written = '';
  const ok = await checkLodge(BASE, { fetchFn: async () => ({ ok: true }), write: (s) => { written += s; } });
  assert.equal(ok, true);
  assert.equal(written, '');
});

test('checkLodge prints the checklist when no lodge answers', async () => {
  let written = '';
  const ok = await checkLodge(BASE, {
    fetchFn: refused,
    pathVar: binDir('uv', 'tmux', 'claude'),
    run: () => ({ status: 1, stdout: '' }),
    write: (s) => { written += s; },
  });
  assert.equal(ok, false);
  assert.match(written, /✓ uv {3}✓ tmux {3}✓ claude {3}✗ woltspace/);
});

test('the tui exits with the checklist, without opening, when no lodge answers', () => {
  const result = spawnSync(process.execPath, ['src/main.js'], {
    cwd: new URL('..', import.meta.url),
    encoding: 'utf8',
    env: { ...process.env, WOLTSPACE_API: 'http://127.0.0.1:9' },
    timeout: 20000,
  });
  assert.equal(result.status, 1, result.stderr);
  assert.match(result.stderr, /lodge answers at http:\/\/127\.0\.0\.1:9/);
});
