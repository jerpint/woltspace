// Attach to a session's real tmux. The tui suspends, the terminal belongs to
// tmux until detach (C-b d), then the list re-renders with a fresh fetch.
//
// native host:    tmux attach -t <slug>
// container:      tmux attach -t <slug>  (TMUX cleared so nested attach works)
// external host:  docker exec -it -u node <container> tmux attach -t <slug>

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

// Bind ONLY the current detach key - never unbind others. Users bind their own
// root keys in tmux.conf.local (e.g. vim-tmux-navigator's C-h/j/k/l/C-\), and
// a hardcoded "retired defaults" unbind list would silently eat them.
function ensureDetachKey(options) {
  const cmd = tmuxCmd(['bind-key', '-n', detachKey(), 'detach-client'], options);
  try {
    spawnSync(cmd[0], cmd.slice(1), { stdio: 'ignore' });
  } catch {
    /* non-fatal - C-b d always works */
  }
}

export function attach(slug, options = {}) {
  ensureDetachKey(options);
  const [cmd, ...args] = attachCommand(slug, options);
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
