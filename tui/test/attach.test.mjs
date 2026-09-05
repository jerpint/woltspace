import test from 'node:test';
import assert from 'node:assert/strict';

import { attachCommand, switchCommand } from '../src/attach.js';

test('same-server entry switches the client with an exact-match target', () => {
  // `=` matters: bare -t prefix-matches, and a session slug that prefixes
  // another session's name would switch to the wrong one.
  assert.deepEqual(
    switchCommand('maple-session'),
    ['tmux', 'switch-client', '-t', '=maple-session'],
  );
});

test('native host attach uses direct inherited-stdio tmux', () => {
  assert.deepEqual(
    attachCommand('maple-session', { isolation: 'host', insideContainer: false }),
    ['tmux', '-u', 'attach', '-t', 'maple-session'],
  );
});

test('in-container attach remains direct', () => {
  assert.deepEqual(
    attachCommand('maple-session', { isolation: 'external', insideContainer: true }),
    ['tmux', '-u', 'attach', '-t', 'maple-session'],
  );
});

test('external lodge reached from host retains Docker compatibility', () => {
  assert.deepEqual(
    attachCommand('maple-session', {
      isolation: 'external', insideContainer: false, container: 'woltspace-test',
    }),
    [
      'docker', 'exec', '-it', '-u', 'node',
      '-e', 'LANG=C.UTF-8', '-e', 'LC_ALL=C.UTF-8',
      'woltspace-test', 'tmux', '-u', 'attach', '-t', 'maple-session',
    ],
  );
});
