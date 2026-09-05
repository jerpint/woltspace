// Attach to a session's real tmux. The tui suspends, the terminal belongs to
// tmux until detach (C-b d), then the list re-renders with a fresh fetch.
//
// native host:    tmux attach -t <slug>
//                 — unless we are already inside tmux on the SAME server, where
//                   attach would nest a client inside a pane (two status bars,
//                   fighting prefixes): then switch-client moves this client to
//                   the session instead, and the detach key switches back.
// container:      tmux attach -t <slug>  (TMUX cleared so nested attach works)
// external host:  docker exec -it -u node <container> tmux attach -t <slug>
//                 (different tmux server — nesting is the only option there)

import { existsSync } from 'node:fs';
import { spawnSync, execSync } from 'node:child_process';

export const inContainer = () => existsSync('/workspace/woltspace/server');

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

// One obvious way home: a detach key bound on the tmux server idempotently
// before each attach (C-b d always works too, for tmux hands). It is nothing
// more than `tmux bind-key -n <key> detach-client`. Override with
// WOLTSPACE_TUI_DETACH (any tmux key name: 'C-]', 'F9', ...).
// Default C-\: single-key, not swallowed by macOS (ctrl-arrows are Mission
// Control shortcuts by default and never reach the terminal).
export const detachKey = () => process.env.WOLTSPACE_TUI_DETACH || 'C-\\';

export const detachLabel = () =>
  detachKey()
    .replace(/^C-/, 'ctrl-')
    .replace(/^M-/, 'alt-')
    .replace(/Left$/, '←')
    .replace(/Right$/, '→');

function tmuxCmd(args, options = {}) {
  const insideContainer = options.insideContainer ?? inContainer();
  const direct = insideContainer || options.isolation === 'host';
  return direct
    ? ['tmux', ...args]
    : ['docker', 'exec', '-u', 'node', options.container || containerName(), 'tmux', ...args];
}

// Moving this client to the session on the same server. `=` forces an exact
// session-name match — bare -t prefix-matches, and a slug that prefixes
// another session's name would switch to the wrong one.
export const switchCommand = (slug) => ['tmux', 'switch-client', '-t', `=${slug}`];

// Bind ONLY the current detach key - never unbind others. Users bind their own
// root keys in tmux.conf.local (e.g. vim-tmux-navigator's C-h/j/k/l/C-\), and
// a hardcoded "retired defaults" unbind list would silently eat them.
// The key's action matches how the session was entered: a nested-free
// switch-client is left with `switch-client -l` (back to where the tui lives),
// a real attach with `detach-client`. Rebound before every entry, so the
// binding always matches the most recent gesture.
function ensureDetachKey(options, action = ['detach-client']) {
  const cmd = tmuxCmd(['bind-key', '-n', detachKey(), ...action], options);
  try {
    spawnSync(cmd[0], cmd.slice(1), { stdio: 'ignore' });
  } catch {
    /* non-fatal - C-b d always works */
  }
}

// True when the caller is inside tmux and <slug> lives on that same server —
// the case where attach would nest. has-session runs with $TMUX intact, so it
// asks the server this client belongs to, the one switch-client would act on.
function sameServerSession(slug) {
  if (!process.env.TMUX) return false;
  try {
    const r = spawnSync('tmux', ['has-session', '-t', `=${slug}`], { stdio: 'ignore' });
    return r.status === 0;
  } catch {
    return false;
  }
}

export function attach(slug, options = {}) {
  const [cmd, ...args] = attachCommand(slug, options);
  if (cmd === 'tmux' && sameServerSession(slug)) {
    ensureDetachKey(options, ['switch-client', '-l']);
    const [sw, ...swArgs] = switchCommand(slug);
    const r = spawnSync(sw, swArgs, { stdio: 'inherit' });
    if (r.error) throw new Error(`switch failed: ${r.error.message}`);
    return r.status ?? 1;
  }
  ensureDetachKey(options);
  const env = { ...process.env };
  delete env.TMUX; // allow attach from inside another tmux
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
