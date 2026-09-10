import test from 'node:test';
import assert from 'node:assert/strict';

import { attachCommand, statusOffCommand, insideTmux } from '../src/attach.js';

test('native host attach uses direct inherited-stdio tmux', () => {
  assert.deepEqual(
    attachCommand('maple-session', { isolation: 'host', insideContainer: false }),
    ['tmux', '-u', 'attach', '-t', 'maple-session'],
  );
});

test('in-container attach is direct regardless of isolation', () => {
  assert.deepEqual(
    attachCommand('maple-session', { insideContainer: true }),
    ['tmux', '-u', 'attach', '-t', 'maple-session'],
  );
});

test('external lodge reached from host retains Docker compatibility', () => {
  assert.deepEqual(
    attachCommand('maple-session', {
      isolation: 'external', insideContainer: false, container: 'woltspace',
    }),
    [
      'docker', 'exec', '-it', '-u', 'node',
      '-e', 'LANG=C.UTF-8', '-e', 'LC_ALL=C.UTF-8',
      'woltspace', 'tmux', '-u', 'attach', '-t', 'maple-session',
    ],
  );
});

test('nested entry quiets the status bar of that session only', () => {
  // The trailing colon names the session: bare `-t maple` would prefix-match
  // `maple-2`, and `=maple` is not accepted by set-option's target parser.
  assert.deepEqual(
    statusOffCommand('maple', { isolation: 'host', insideContainer: false }),
    ['tmux', 'set-option', '-t', 'maple:', 'status', 'off'],
  );
  assert.deepEqual(
    statusOffCommand('maple', { isolation: 'external', insideContainer: false, container: 'w' }),
    ['docker', 'exec', '-u', 'node', 'w', 'tmux', 'set-option', '-t', 'maple:', 'status', 'off'],
  );
});

test('nesting is decided by $TMUX, the only thing that says we are in a pane', () => {
  assert.equal(insideTmux({}), false);
  assert.equal(insideTmux({ TMUX: '/tmp/tmux-501/default,1,0' }), true);
});
