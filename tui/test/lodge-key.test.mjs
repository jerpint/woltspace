import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { lodgeKey } from '../src/api.js';

test('the cockpit sends the wolt key in a session, the owner key outside, an explicit key first', () => {
  const root = mkdtempSync(join(tmpdir(), 'lodge-key-'));
  mkdirSync(join(root, '.space', 'run', 'lodge'), { recursive: true });
  writeFileSync(join(root, '.space', 'run', 'lodge', 'owner.key'), 'owner-key\n');
  writeFileSync(join(root, '.space', 'run', 'lodge', 'wolt.key'), 'wolt-key\n');

  assert.equal(lodgeKey({ WOLTSPACE_WOLTS_DIR: root }), 'owner-key');
  assert.equal(lodgeKey({ WOLTSPACE_WOLTS_DIR: root, WOLTSPACE_WOLT_SESSION: 's-abc123' }), 'wolt-key');
  assert.equal(lodgeKey({ WOLTSPACE_WOLTS_DIR: root, WOLTSPACE_LODGE_KEY: 'explicit' }), 'explicit');
  assert.equal(lodgeKey({}), '');
});
