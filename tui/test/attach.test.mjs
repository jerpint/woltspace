import test from 'node:test';
import assert from 'node:assert/strict';

import {
  attachCommand, statusOffCommand, detachByte, detachKey, detachLabel, insideTmux,
  splitAtDetach, toBytes, clientTtyForPid, DEFAULT_DETACH,
} from '../src/attach.js';

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

test('in-pane entry quiets the status bar of that session only', () => {
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

test('detach key names map to the byte a terminal sends', () => {
  assert.equal(detachByte('C-]'), 0x1d);
  assert.equal(detachByte('C-\\'), 0x1c);
  assert.equal(detachByte('C-a'), 0x01);
  assert.equal(detachByte('C-Z'), 0x1a);
  assert.equal(detachByte('^]'), 0x1d);
  // Not control chords: nothing in front of the nested client can catch them.
  assert.equal(detachByte('F9'), null);
  assert.equal(detachByte('M-d'), null);
  assert.equal(detachByte(''), null);
  assert.equal(detachByte(undefined), null);
});

test('WOLTSPACE_TUI_DETACH is honoured only when it can be intercepted', () => {
  assert.equal(detachKey({}), DEFAULT_DETACH);
  assert.equal(detachKey({ WOLTSPACE_TUI_DETACH: 'C-q' }), 'C-q');
  assert.equal(detachKey({ WOLTSPACE_TUI_DETACH: 'F9' }), DEFAULT_DETACH);
});

test('the footer names the real way back for where the tui runs', () => {
  assert.equal(insideTmux({}), false);
  assert.equal(insideTmux({ TMUX: '/tmp/tmux-501/default,1,0' }), true);
  assert.equal(detachLabel({}), 'prefix d');
  assert.equal(detachLabel({ TMUX: 'x' }), 'ctrl-]');
  assert.equal(detachLabel({ TMUX: 'x', WOLTSPACE_TUI_DETACH: 'C-q' }), 'ctrl-q');
  assert.equal(detachLabel({ TMUX: 'x', WOLTSPACE_TUI_DETACH: 'F9' }), 'ctrl-]');
});

test('C-[ is refused: it is ESC, the first byte of every arrow key', () => {
  assert.equal(detachByte('C-['), null);
  assert.equal(detachKey({ WOLTSPACE_TUI_DETACH: 'C-[' }), DEFAULT_DETACH);
  // Nothing inherited from Object.prototype is a control chord either.
  assert.equal(detachByte('C-constructor'), null);
  assert.equal(detachKey({ WOLTSPACE_TUI_DETACH: '  C-q  ' }), 'C-q');
});

test('input is read as bytes, whatever ink left the stream decoding as', () => {
  // Ink sets stdin to utf8 and never unsets it, so chunks arrive as strings.
  assert.deepEqual(toBytes('\u001d'), Buffer.from([0x1d]));
  assert.deepEqual(toBytes(Buffer.from([0x1d])), Buffer.from([0x1d]));
  assert.equal(splitAtDetach('\u001d', 0x1d).detached, true);
  assert.equal(splitAtDetach(Buffer.from([0x1d]), 0x1d).detached, true);
  // The string spelling of the byte is not the byte: pasting "29" is input.
  assert.equal(splitAtDetach('paste 29 here', 0x1d).detached, false);
});

test('a chunk carrying the detach byte still delivers what came before it', () => {
  const { before, detached } = splitAtDetach('yes\u001dnope', 0x1d);
  assert.equal(detached, true);
  assert.equal(before.toString('utf8'), 'yes');
  const plain = splitAtDetach('hello', 0x1d);
  assert.equal(plain.detached, false);
  assert.equal(plain.before.toString('utf8'), 'hello');
});

test('multibyte input survives the round trip and never false-detaches', () => {
  // 🦝 is f0 9f a6 9d - every continuation byte is >= 0x80, so no control
  // chord can be found inside a character.
  const text = 'le 🦝 arrive — ünïcode';
  assert.equal(splitAtDetach(text, 0x1d).detached, false);
  assert.equal(splitAtDetach(text, 0x1d).before.toString('utf8'), text);
  for (let b = 1; b < 0x20; b++) {
    assert.equal(splitAtDetach(text, b).detached, false, `byte ${b} matched inside text`);
  }
});

test('a detach with no interceptable key forwards everything', () => {
  assert.equal(splitAtDetach('anything', null).detached, false);
  assert.equal(splitAtDetach('anything', null).before.toString('utf8'), 'anything');
});

test('the nested client is found by pid, never by position', () => {
  const listing = [
    '4310 /dev/ttys009',
    '4477 /dev/ttys012',
    '44 /dev/ttys001',
    '',
  ].join('\n');
  assert.equal(clientTtyForPid(listing, 4477), '/dev/ttys012');
  assert.equal(clientTtyForPid(listing, 44), '/dev/ttys001');
  // A pid that is only a prefix of a listed one is not that client.
  assert.equal(clientTtyForPid(listing, 431), null);
  assert.equal(clientTtyForPid(listing, 9999), null);
  assert.equal(clientTtyForPid('', 4477), null);
  assert.equal(clientTtyForPid(undefined, 4477), null);
});
