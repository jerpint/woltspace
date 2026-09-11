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

test('native spawn roots the wolt in its own home, not the launch directory', () => {
  assert.deepEqual(
    spawnTarget({
      supports_host_workdirs: true,
      default_harness: 'claude',
      default_execution_policy: 'prompt',
      default_execution_policies: { claude: 'prompt', codex: 'guarded' },
    }, { home: '/wolts/maple', harness: 'codex' }, '/src/project'),
    {
      workdir: null,
      displayWorkdir: '/wolts/maple',
      executionPolicy: 'guarded',
      supportsHostWorkdirs: true,
    },
  );
});

test('a wolt policy pin wins over the harness default on every future spawn', () => {
  const target = spawnTarget({
    supports_host_workdirs: true,
    default_harness: 'codex',
    default_execution_policy: 'guarded',
    default_execution_policies: { claude: 'prompt', codex: 'guarded' },
  }, {
    home: '/wolts/maple', harness: 'codex', execution_policy: 'auto',
  }, '/src/project');

  assert.equal(target.executionPolicy, 'auto');
});

test('container spawn keeps the existing wolt-home default', () => {
  assert.deepEqual(
    spawnTarget({ supports_host_workdirs: false, default_execution_policy: 'auto' },
      { home: '/workspace/wolts/maple' }, '/host/project'),
    {
      workdir: null,
      displayWorkdir: '/workspace/wolts/maple',
      executionPolicy: 'auto',
      supportsHostWorkdirs: false,
    },
  );
});

// Two wolts woken from the same terminal must not share a root. Before this,
// native handed both of them the one directory the TUI was launched from.
test('two wolts woken from one launch directory land in their own homes', () => {
  const caps = {
    supports_host_workdirs: true,
    default_harness: 'codex',
    default_execution_policy: 'guarded',
    default_execution_policies: { claude: 'prompt', codex: 'guarded' },
  };
  const a = spawnTarget(caps, { home: '/wolts/maple' }, '/wolts/birch');
  const b = spawnTarget(caps, { home: '/wolts/birch' }, '/wolts/birch');
  assert.equal(a.workdir, null);
  assert.equal(b.workdir, null);
  assert.notEqual(a.displayWorkdir, b.displayWorkdir);
});

test('a spawn with no wolt in hand still never borrows the launch directory', () => {
  const target = spawnTarget({ supports_host_workdirs: true }, null, '/src/project');
  assert.equal(target.workdir, null);
  assert.equal(target.displayWorkdir, 'wolt home');
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
