import test from 'node:test';
import assert from 'node:assert/strict';

import { createWoltAction, validateWoltName } from '../src/create-wolt.js';

test('wolt name validation mirrors the server contract', () => {
  assert.equal(validateWoltName('maple-2'), '');
  assert.match(validateWoltName('2maple'), /start with a letter/);
  assert.match(validateWoltName('Maple Tree'), /lowercase letters/);
  assert.match(validateWoltName('a'.repeat(21)), /20 characters/);
});

test('native create action carries confirmed cwd policy and isolation', () => {
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
    workdir: '/src/project',
    executionPolicy: 'prompt',
    isolation: 'host',
  });
});
