// The one version check between the two halves lives on this side: the tui
// declares the minimum woltspace it needs, and nothing in the lodge inspects
// the tui's version. These tests pin the comparison, including the two cases
// that bite in the field — pre-release suffixes and a lodge too old to report
// a version at all.

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  ASSUMED_LODGE_VERSION,
  lodgeSatisfies,
  minLodgeVersion,
  packageVersion,
  parseVersion,
  versionBanner,
} from '../src/version.js';

test('the declared minimum is a plain release version', () => {
  assert.match(minLodgeVersion, /^\d+\.\d+\.\d+$/);
  assert.equal(ASSUMED_LODGE_VERSION, '0.5.0');
});

test('parseVersion drops pre-release and build suffixes', () => {
  assert.deepEqual(parseVersion('0.5.1'), [0, 5, 1]);
  assert.deepEqual(parseVersion('v1.2.3'), [1, 2, 3]);
  assert.deepEqual(parseVersion('0.6.0-rc.1'), [0, 6, 0]); // semver
  assert.deepEqual(parseVersion('0.6.0rc5'), [0, 6, 0]); // PEP 440
  assert.deepEqual(parseVersion('0.6.0+dev'), [0, 6, 0]);
  assert.deepEqual(parseVersion('0.6'), [0, 6, 0]);
  assert.deepEqual(parseVersion('junk'), [0, 0, 0]);
});

test('a newer or equal lodge satisfies the minimum', () => {
  assert.equal(lodgeSatisfies('0.5.0', '0.5.0'), true);
  assert.equal(lodgeSatisfies('0.5.1', '0.5.0'), true);
  assert.equal(lodgeSatisfies('0.6.0', '0.5.9'), true);
  assert.equal(lodgeSatisfies('1.0.0', '0.9.9'), true);
  assert.equal(lodgeSatisfies('0.5.0-rc.1', '0.5.0'), true, 'an rc counts as the release');
});

test('an older lodge does not', () => {
  assert.equal(lodgeSatisfies('0.4.9', '0.5.0'), false);
  assert.equal(lodgeSatisfies('0.5.0', '0.5.1'), false);
  assert.equal(lodgeSatisfies('0.9.9', '1.0.0'), false);
  assert.equal(lodgeSatisfies('junk', '0.5.0'), false);
});

test('a lodge that reports no version is read as the last release without the field', () => {
  for (const missing of [undefined, null, '', '   ']) {
    assert.equal(lodgeSatisfies(missing, '0.5.0'), true);
    assert.equal(lodgeSatisfies(missing, '0.5.1'), false);
  }
});

test('this tui does not require a lodge newer than itself', () => {
  assert.equal(lodgeSatisfies(packageVersion, minLodgeVersion), true);
});

test('the banner names the fix, and stays silent on a matched pair', () => {
  assert.equal(versionBanner(packageVersion), null);
  assert.equal(versionBanner(undefined), null, 'an unversioned lodge is fine today');

  const tooOld = versionBanner('0.4.9', '9.9.9');
  assert.match(tooOld, /needs woltspace >= /);
  assert.match(tooOld, /uv tool install/);
  assert.match(tooOld, /0\.4\.9/);

  const tooNew = versionBanner('0.9.0', '0.5.1');
  assert.match(tooNew, /newer than this tui/);
  assert.match(tooNew, /@woltspace\/tui@latest/);

  assert.equal(versionBanner('0.5.9', '0.5.1'), null, 'a patch ahead is not news');
});
