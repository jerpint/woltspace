// Attach to a session's real tmux. The tui suspends, the terminal belongs to
// the session until you come back, then the list re-renders with a fresh fetch.
//
// outside tmux:  tmux attach -t <slug> with inherited stdio - the classic
//                client. Your prefix + d detaches, as always.
// inside tmux:   the wolt renders IN THE PANE you are in. The wolt's session
//                stays where it is (telegram delivery, the web tui and the
//                vulture all address it by session name), and this pane runs
//                a nested client to it through a pty the tui owns - the same
//                mechanism the web tui uses to put a session in a browser. Its
//                status bar is switched off so it reads as the agent, not as
//                tmux-in-tmux. Your outer tmux still sees your prefix and pane
//                keys first; the one key the tui intercepts before the nested
//                client (ctrl-], docker style) brings the list back.
// container:     same two modes. external host: same two modes over
//                `docker exec` into the lodge container.
//
// The tui never binds or unbinds keys on a tmux server. Users own their key
// tables - an earlier detach-key rebinding ate the root binding of the person
// who reported that entering a session teleported them out of their windows.

import { existsSync } from 'node:fs';
import { spawnSync, execSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { ensureSpawnHelperExecutable } from './pty-service.js';

const require = createRequire(import.meta.url);

export const inContainer = () => existsSync('/workspace/woltspace/server');

export const insideTmux = (env = process.env) => Boolean(env.TMUX);

export function containerName() {
  if (process.env.WOLTSPACE_CONTAINER) return process.env.WOLTSPACE_CONTAINER;
  try {
    const names = execSync("docker ps --format '{{.Names}}' --filter name=woltspace", {
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
    })
      .trim()
      .split('\n')
      .filter(Boolean);
    if (names.length) return names[0];
  } catch {
    /* docker absent or not running - fall through */
  }
  return 'woltspace';
}

export function attachCommand(slug, options = {}) {
  const insideContainer = options.insideContainer ?? inContainer();
  const direct = insideContainer || options.isolation === 'host';
  // -u + a UTF-8 locale: without LANG in the docker-exec'd process, the tmux
  // client decides the terminal can't render wide glyphs and strips them.
  // Mirrors the proven `woltspace chat --session` invocation.
  return direct
    ? ['tmux', '-u', 'attach', '-t', slug]
    : ['docker', 'exec', '-it', '-u', 'node',
       '-e', 'LANG=C.UTF-8', '-e', 'LC_ALL=C.UTF-8',
       options.container || containerName(), 'tmux', '-u', 'attach', '-t', slug];
}

function tmuxCmd(args, options = {}) {
  const insideContainer = options.insideContainer ?? inContainer();
  const direct = insideContainer || options.isolation === 'host';
  return direct
    ? ['tmux', ...args]
    : ['docker', 'exec', '-u', 'node', options.container || containerName(), 'tmux', ...args];
}

// A wolt session shown inside someone's pane should look like the agent, not
// like a second tmux. `status` is a session option, so this only touches the
// wolt's session (`slug:` = that session, never a prefix match), and the web
// tui shows the same session without a bar either way.
export const statusOffCommand = (slug, options = {}) =>
  tmuxCmd(['set-option', '-t', `${slug}:`, 'status', 'off'], options);

// --- the way back --------------------------------------------------------
//
// Inside tmux the tui owns the pty in front of the nested client, so it can
// take one key before tmux ever sees it. Outside tmux nothing sits between
// the user and tmux, and the ordinary prefix + d is the answer.

export const DEFAULT_DETACH = 'C-]';

// `[` is deliberately absent: C-[ is ESC, the first byte of every arrow key
// and every other escape sequence, so honouring it would detach on the first
// cursor key. A key we cannot take without eating the keyboard is not a key
// we can offer.
const CTRL_PUNCT = { '@': 0x00, '\\': 0x1c, ']': 0x1d, '^': 0x1e, '_': 0x1f, ' ': 0x00 };

// tmux-style key name -> the single byte a terminal sends for it, or null
// when it is not a control chord the pty can intercept (F9, M-x, ...).
export function detachByte(key) {
  const m = /^(?:C-|\^)(.)$/.exec(key || '');
  if (!m) return null;
  const ch = m[1];
  if (/[a-zA-Z]/.test(ch)) return ch.toLowerCase().charCodeAt(0) - 96;
  if (Object.hasOwn(CTRL_PUNCT, ch)) return CTRL_PUNCT[ch];
  return null;
}

// WOLTSPACE_TUI_DETACH names the key. Only a control chord can be caught in
// front of the nested client, so anything else falls back to the default.
export function detachKey(env = process.env) {
  const wanted = env.WOLTSPACE_TUI_DETACH?.trim();
  return wanted && detachByte(wanted) !== null ? wanted : DEFAULT_DETACH;
}

export function detachLabel(env = process.env) {
  if (!insideTmux(env)) return 'prefix d';
  return detachKey(env).replace(/^C-/, 'ctrl-').replace(/^\^/, 'ctrl-');
}

// Ink puts process.stdin in utf8 mode and never takes it back off, so by the
// time we mirror the pane the "data" chunks are decoded strings, not Buffers.
// A string carries none of the byte semantics this needs: `'\x1d'.includes(29)`
// searches for the TEXT "29", which is false for the real ctrl-] and true for
// anyone who pastes the number 29. Normalise to bytes first, once.
export const toBytes = (chunk) =>
  (typeof chunk === 'string' ? Buffer.from(chunk, 'utf8') : Buffer.from(chunk));

// Split an input chunk at the detach byte. Everything typed or pasted before
// it still reaches the agent - a chunk is not one keystroke, it is whatever
// the terminal handed us in one read, and dropping it loses real input. The
// byte is always below 0x80, and no continuation byte of a multi-byte UTF-8
// character is, so a byte search can never fire in the middle of a character.
export function splitAtDetach(chunk, detach) {
  const bytes = toBytes(chunk);
  if (detach === null || detach === undefined) return { before: bytes, detached: false };
  const at = bytes.indexOf(detach);
  if (at < 0) return { before: bytes, detached: false };
  return { before: bytes.subarray(0, at), detached: true };
}

// `tmux list-clients -F '#{client_pid} #{client_tty}'` -> the tty of the client
// running as <pid>, or null. Field order matters more than it looks: a tty path
// can hold anything but a space, a pid cannot, so the pid is the anchor.
export function clientTtyForPid(listing, pid) {
  for (const line of String(listing || '').split('\n')) {
    const sep = line.indexOf(' ');
    if (sep < 0 || line.slice(0, sep) !== String(pid)) continue;
    const tty = line.slice(sep + 1).trim();
    if (tty) return tty;
  }
  return null;
}

// A tmux client that leaves on its own winds the terminal back down for us,
// whichever way it left. One we had to kill does not: the pane stays on the
// alternate screen with mouse reporting and bracketed paste still on and the
// cursor hidden. So this is for the kill path only - sent unconditionally it
// would also leave-the-alternate-screen over a client's last words, and the
// "can't find session" line for a session that is gone is exactly the message
// the user needs to see.
const RESTORE = '\x1b[?1049l\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l'
  + '\x1b[?2004l\x1b[?7h\x1b[0m\x1b[?25h';

// --- entry ---------------------------------------------------------------

export async function attach(slug, options = {}) {
  const [cmd, ...args] = attachCommand(slug, options);
  const env = { ...process.env };
  delete env.TMUX; // a nested client is exactly what the in-pane mode wants

  if (insideTmux()) {
    const pty = loadPty();
    if (pty) {
      quietStatus(slug, options, env);
      const code = await attachInPane(pty, cmd, args, env, {
        detach: detachByte(detachKey()),
        direct: cmd === 'tmux',
      });
      if (code !== null) return code;
      // The pty could not be started at all. Rather than drop the user back on
      // a list that just refused to open anything, fall through to the plain
      // client below - it is worse to live in, but it does open.
    }
    console.error(
      'this pane cannot host the session (node-pty unavailable); attaching a nested '
      + 'tmux client instead - your outer tmux takes the prefix first, so it is '
      + 'prefix prefix d to come back',
    );
  }

  const r = spawnSync(cmd, args, { stdio: 'inherit', env });
  if (r.error?.code === 'ENOENT') {
    if (cmd === 'tmux') {
      throw new Error('tmux is missing; install tmux, then run woltspace doctor');
    }
    throw new Error('Docker is missing; install Docker or run the lodge natively');
  }
  if (r.error) throw new Error(`attach failed: ${r.error.message}`);
  return r.status ?? 1;
}

// Runs with the same TMUX-free environment as the client, and for the same
// reason: with $TMUX still set, tmux talks to the server the USER is sitting
// in, not the one the wolt lives on. On the socket the tui was launched from
// the slug is simply not a session (the bar stayed on, silently); on a server
// where some session of the user's happened to share the name, it would have
// turned that one's status bar off instead.
function quietStatus(slug, options, env) {
  const [cmd, ...args] = statusOffCommand(slug, options);
  try {
    // Timed: a docker exec against a wedged daemon must not hold the keystroke.
    spawnSync(cmd, args, { stdio: 'ignore', env, timeout: 5000 });
  } catch {
    /* cosmetic - the session still works with a bar */
  }
}

function loadPty() {
  try {
    const pty = require('node-pty');
    // node-pty ships the darwin spawn-helper without the executable bit;
    // the web bridge heals it at boot, but the in-pane path may run first.
    ensureSpawnHelperExecutable({ log: () => {} });
    return pty;
  } catch {
    return null;
  }
}

// Run the attach command on a pty, mirror bytes both ways, intercept the
// detach byte, and track the pane's size. Resolves to the client's exit code
// (0 on a detach we initiated), or to null if the pty never started, which
// leaves the caller free to try a plainer way in.
function attachInPane(pty, cmd, args, env, { detach, direct }) {
  return new Promise((resolve) => {
    const { stdin, stdout } = process;
    const size = () => ({ cols: stdout.columns || 80, rows: stdout.rows || 24 });
    let term;
    try {
      term = pty.spawn(cmd, args, {
        name: env.TERM || 'xterm-256color',
        ...size(),
        cwd: process.cwd(),
        env,
      });
    } catch (e) {
      console.error(`in-pane attach failed: ${e.message}`);
      resolve(null);
      return;
    }

    const wasRaw = Boolean(stdin.isRaw);
    let done = false;
    let exited = false;
    let onPtyExit = null;

    // The client's farewell - leaving the alternate screen, repainting what
    // was under it - is pty output like any other, so output stays mirrored
    // until the pty is really gone. Disposing the subscription first (as the
    // first draft did) left the pane frozen on the wolt's last frame.
    const ptyGone = (ms) => new Promise((r) => {
      if (exited) return r();
      const timer = setTimeout(() => { onPtyExit = null; r(); }, ms);
      onPtyExit = () => { clearTimeout(timer); r(); };
    });

    const finish = async (code, detached) => {
      if (done) return;
      done = true;
      stdin.off('data', onInput);
      stdout.off('resize', onResize);
      let killed = false;
      if (detached) {
        // Ask the server to drop this client so it restores the screen itself;
        // only if that fails do we pull the pty out from under it. Either way
        // the wolt session and every other client on it are untouched.
        killed = !detachClient(term.pid, env, direct);
        if (killed) {
          try { term.kill(); } catch { /* already gone */ }
        }
        await ptyGone(1500);
      }
      outSub.dispose();
      exitSub.dispose();
      if (killed && stdout.isTTY) stdout.write(RESTORE);
      if (stdin.isTTY) stdin.setRawMode(wasRaw);
      // Back to paused: ink's next render listens for "readable", and a stream
      // left flowing with no data listener would throw those bytes away.
      stdin.pause();
      resolve(code);
    };

    const onInput = (chunk) => {
      const { before, detached } = splitAtDetach(chunk, detach);
      if (before.length) {
        try { term.write(before); } catch { /* client gone */ }
      }
      if (detached) finish(0, true);
    };
    const onResize = () => {
      const { cols, rows } = size();
      try { term.resize(cols, rows); } catch { /* client gone */ }
    };
    const outSub = term.onData((data) => stdout.write(data));
    const exitSub = term.onExit(({ exitCode }) => {
      exited = true;
      onPtyExit?.();
      // The session may simply not be there ("can't find session", exit 1).
      // Reporting that code is what sends the caller back to the list with a
      // reason instead of sitting on a dead pty.
      finish(exitCode ?? 0, false);
    });

    if (stdin.isTTY) stdin.setRawMode(true);
    stdin.resume();
    stdin.on('data', onInput);
    // Only a tty ever emits resize; on anything else this listener is inert
    // rather than wrong, and size() has already fallen back to 80x24.
    stdout.on('resize', onResize);
  });
}

// The nested client is the process on our pty; tmux knows it by pid. Only ask
// the server we actually spawned into: over `docker exec` the pid on this side
// belongs to docker, the tmux client's pid lives in the container, and the
// host's tmux would be the wrong server answering about the wrong process - so
// that path goes straight to the kill fallback. Both calls are timed, because
// a wedged tmux must not strand the pane it is holding.
function detachClient(pid, env, direct) {
  if (!direct) return false;
  try {
    const list = spawnSync('tmux', ['list-clients', '-F', '#{client_pid} #{client_tty}'], {
      encoding: 'utf8', env, timeout: 2000,
    });
    if (list.status !== 0) return false;
    const tty = clientTtyForPid(list.stdout, pid);
    if (!tty) return false;
    // No shell: a tty path is data, and building a command string out of it is
    // how a name with a quote in it turns into someone else's command.
    const r = spawnSync('tmux', ['detach-client', '-t', tty], {
      env, timeout: 2000, stdio: 'ignore',
    });
    return r.status === 0;
  } catch {
    return false;
  }
}
