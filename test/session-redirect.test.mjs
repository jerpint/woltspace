/**
 * Tests for session redirect feature (new_session / Mode B).
 *
 * Run with: node --test test/session-redirect.test.mjs
 *
 * These tests cover the server-side redirect file mechanism without
 * requiring a running server or tmux.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, existsSync, readFileSync, unlinkSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';

// ── Helpers (mirrors the server logic inline for unit testing) ──────────────

function sanitizeSession(name) {
  return (name || '').replace(/[^a-zA-Z0-9_-]/g, '').slice(0, 64) || 'main';
}

function makeHelpers(stateDir) {
  const redirectFile = (session) =>
    join(stateDir, `redirect-${sanitizeSession(session)}.json`);
  const currentUrlFile = (session) =>
    join(stateDir, `current-url-${sanitizeSession(session)}.json`);

  function setRedirect(from, to) {
    const safeFrom = sanitizeSession(from);
    const safeTo = sanitizeSession(to);
    writeFileSync(redirectFile(safeFrom), JSON.stringify({ from: safeFrom, to: safeTo, t: Date.now() }));
  }

  function readCurrentMeta(session) {
    const f = currentUrlFile(session);
    const data = existsSync(f)
      ? JSON.parse(readFileSync(f, 'utf8'))
      : { url: null, updated: 0 };
    // Consume pending redirect
    const rf = redirectFile(session);
    if (existsSync(rf)) {
      try {
        const rdata = JSON.parse(readFileSync(rf, 'utf8'));
        data.redirect = rdata.to;
        unlinkSync(rf);
      } catch {}
    }
    return data;
  }

  return { setRedirect, readCurrentMeta, redirectFile };
}

// ── Tests ───────────────────────────────────────────────────────────────────

test('redirect file is written and consumed on first read', () => {
  const stateDir = mkdtempSync(join(tmpdir(), 'woltspace-test-'));
  const { setRedirect, readCurrentMeta, redirectFile } = makeHelpers(stateDir);

  setRedirect('neowolt-abc123', 'neowolt-xyz789');

  // File should exist before read
  assert.ok(existsSync(redirectFile('neowolt-abc123')), 'redirect file should exist');

  const meta = readCurrentMeta('neowolt-abc123');
  assert.equal(meta.redirect, 'neowolt-xyz789', 'redirect field should be present');

  // File should be consumed (deleted) after read
  assert.ok(!existsSync(redirectFile('neowolt-abc123')), 'redirect file should be consumed after read');
});

test('second read returns no redirect (consumed after first)', () => {
  const stateDir = mkdtempSync(join(tmpdir(), 'woltspace-test-'));
  const { setRedirect, readCurrentMeta } = makeHelpers(stateDir);

  setRedirect('neowolt-abc123', 'neowolt-xyz789');
  readCurrentMeta('neowolt-abc123'); // consume
  const meta2 = readCurrentMeta('neowolt-abc123');

  assert.equal(meta2.redirect, undefined, 'no redirect on second read');
});

test('meta without redirect returns url and no redirect field', () => {
  const stateDir = mkdtempSync(join(tmpdir(), 'woltspace-test-'));
  const { readCurrentMeta } = makeHelpers(stateDir);
  const meta = readCurrentMeta('some-session');
  assert.equal(meta.url, null);
  assert.equal(meta.redirect, undefined);
});

test('sanitizeSession strips dangerous characters', () => {
  assert.equal(sanitizeSession('neowolt-abc_123'), 'neowolt-abc_123');
  assert.equal(sanitizeSession('foo/../etc/passwd'), 'fooetcpasswd');
  assert.equal(sanitizeSession(''), 'main');
  assert.equal(sanitizeSession(null), 'main');
  assert.equal(sanitizeSession('a'.repeat(100)), 'a'.repeat(64));
});

test('redirect with special chars in session name is sanitized', () => {
  const stateDir = mkdtempSync(join(tmpdir(), 'woltspace-test-'));
  const { setRedirect, readCurrentMeta } = makeHelpers(stateDir);

  // Session names with spaces/slashes get sanitized — redirect still works
  setRedirect('neowolt abc', 'neowolt-new');
  const meta = readCurrentMeta('neowolt abc');
  assert.equal(meta.redirect, 'neowolt-new');
});
