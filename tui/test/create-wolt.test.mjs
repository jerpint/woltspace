import test from 'node:test';
import assert from 'node:assert/strict';

import { createWoltAction, validateWoltName } from '../src/create-wolt.js';

test('wolt name validation mirrors the server contract', () => {
  assert.equal(validateWoltName('maple-2'), '');
  assert.match(validateWoltName('2maple'), /start with a letter/);
  assert.match(validateWoltName('Maple Tree'), /lowercase letters/);
  assert.match(validateWoltName('a'.repeat(21)), /20 characters/);
});

// A wolt created from a checkout used to be rooted in that checkout. It now
// starts in <wolts_dir>/<name>, the home the lodge just made for it.
test('native create action starts the new wolt in its own home', () => {
  assert.deepEqual(createWoltAction(
    'maple', 'raccoon', {
      isolation: 'host',
      supports_host_workdirs: true,
      default_execution_policy: 'prompt',
    }, '/src/project',
  ), {
    type: 'create',
    name: 'maple',
    woltType: 'raccoon',
    workdir: null,
    executionPolicy: 'prompt',
    isolation: 'host',
  });
});
