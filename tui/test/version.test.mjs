import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';

import { packageName, packageVersion, versionRecord } from '../src/version.js';

const manifest = JSON.parse(readFileSync(new URL('../package.json', import.meta.url)));

test('runtime identity exactly matches the scoped npm manifest', () => {
  assert.equal(packageName, '@woltspace/tui');
  assert.equal(packageName, manifest.name);
  assert.equal(packageVersion, manifest.version);
  assert.deepEqual(versionRecord(), {
    name: manifest.name,
    version: manifest.version,
    binary: 'woltspace-tui',
  });
});

test('binary exposes machine-readable package identity', () => {
  const result = spawnSync(process.execPath, ['src/main.js', '--version', '--json'], {
    cwd: new URL('..', import.meta.url),
    encoding: 'utf8',
  });
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), versionRecord());
});
