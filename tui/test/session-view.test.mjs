import test from 'node:test';
import assert from 'node:assert/strict';

import {
  agentAlive, inactiveCount, sessionPolicy, sessionWorkdir, spawnTarget,
} from '../src/session-view.js';

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

// --- liveness -------------------------------------------------------------
//
// A tmux session outlives its agent. Enter must key off the agent, or it
// attaches the human to the login shell left holding their session open.

test('a stale agentless tmux session is not alive', () => {
  const husk = { name: 'a', tmux_alive: true, agent_alive: false, alive: false };
  assert.equal(agentAlive(husk), false);
});

test('a session with an agent in it is alive', () => {
  assert.equal(agentAlive({ tmux_alive: true, agent_alive: true, alive: true }), true);
});

test('an older lodge that only sends alive is still understood', () => {
  assert.equal(agentAlive({ alive: true }), true);
  assert.equal(agentAlive({ alive: false }), false);
  assert.equal(agentAlive({}), false);
  assert.equal(agentAlive(null), false);
});

test('agent_alive wins over a stale alive field', () => {
  assert.equal(agentAlive({ alive: true, agent_alive: false }), false);
});

test('the hidden count is what the default filter drops', () => {
  const list = [
    { name: 'live', agent_alive: true },
    { name: 'husk', tmux_alive: true, agent_alive: false },
    { name: 'gone', tmux_alive: false, agent_alive: false },
  ];
  assert.equal(inactiveCount(list), 2);
  assert.equal(inactiveCount([]), 0);
  assert.equal(inactiveCount(undefined), 0);
});
