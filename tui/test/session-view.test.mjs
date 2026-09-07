import test from 'node:test';
import assert from 'node:assert/strict';

import { sessionPolicy, sessionWorkdir, spawnTarget } from '../src/session-view.js';

test('session view prefers canonical target metadata', () => {
  const session = {
    target: { canonical_workdir: '/repo/new' },
    workdir: '/repo/flat',
    dir: '/repo/legacy',
    execution_policy: { mode: 'prompt' },
  };
  assert.equal(sessionWorkdir(session), '/repo/new');
  assert.equal(sessionPolicy(session), 'prompt');
});

test('legacy session view keeps old dir and implicit Auto visible', () => {
  assert.equal(sessionWorkdir({ dir: '/old/repo' }), '/old/repo');
  assert.equal(sessionPolicy({}), 'auto');
});

test('native spawn targets the TUI launch directory with prompt policy', () => {
  assert.deepEqual(
    spawnTarget({ supports_host_workdirs: true, default_execution_policy: 'prompt' },
      { home: '/wolts/maple' }, '/src/project'),
    { workdir: '/src/project', displayWorkdir: '/src/project', executionPolicy: 'prompt' },
  );
});

test('container spawn keeps the existing wolt-home default', () => {
  assert.deepEqual(
    spawnTarget({ supports_host_workdirs: false, default_execution_policy: 'auto' },
      { home: '/workspace/wolts/maple' }, '/host/project'),
    { workdir: null, displayWorkdir: '/workspace/wolts/maple', executionPolicy: 'auto' },
  );
});
