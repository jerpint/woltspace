import test from 'node:test';
import assert from 'node:assert/strict';

import { spawnSession, createWolt } from '../src/api.js';
import { spawnTarget } from '../src/session-view.js';
import { createWoltAction } from '../src/create-wolt.js';

test('spawn and create defer policy to the server while preserving explicit overrides', async (t) => {
  const requests = [];
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    requests.push({ path: new URL(url).pathname, body: JSON.parse(options.body) });
    return { ok: true, json: async () => ({ name: 'maple-session' }) };
  });

  for (const native of [true, false]) {
    const caps = {
      supports_host_workdirs: native,
      default_execution_policy: native ? 'prompt' : 'auto',
    };
    const target = spawnTarget(caps, { home: '/wolts/maple' }, '/repo');
    await spawnSession('maple', target.workdir, target.executionPolicy);
    assert.deepEqual(requests.at(-1), {
      path: '/sessions/new/lodge', body: { wolt: 'maple' },
    });

    const action = createWoltAction('maple', 'raccoon', caps, '/repo');
    await createWolt(action.name, action.woltType, action.workdir, action.executionPolicy);
    assert.deepEqual(requests.at(-1), {
      path: '/sessions/new/create', body: { name: 'maple', type: 'raccoon' },
    });
  }

  for (const policy of ['prompt', 'auto', 'guarded']) {
    await spawnSession('maple', '/repo', policy);
    assert.deepEqual(requests.at(-1).body, {
      wolt: 'maple', workdir: '/repo', execution_policy: policy,
    });
    await createWolt('maple', 'raccoon', '/repo', policy);
    assert.deepEqual(requests.at(-1).body, {
      name: 'maple', type: 'raccoon', workdir: '/repo', execution_policy: policy,
    });
  }
});
