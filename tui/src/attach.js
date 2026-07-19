// Attach to a session's real tmux. The tui suspends, the terminal belongs to
// tmux until detach (C-b d), then the list re-renders with a fresh fetch.
//
// host mode:      docker exec -it -u node <container> tmux attach -t <slug>
// container mode: tmux attach -t <slug>  (TMUX cleared so nested attach works)

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

export function attachCommand(slug) {
  // -u + a UTF-8 locale: without LANG in the docker-exec'd process, the tmux
  // client decides the terminal can't render wide glyphs and strips them.
  // Mirrors the proven `woltspace chat --session` invocation.
  return inContainer()
    ? ['tmux', '-u', 'attach', '-t', slug]
    : ['docker', 'exec', '-it', '-u', 'node',
       '-e', 'LANG=C.UTF-8', '-e', 'LC_ALL=C.UTF-8',
       containerName(), 'tmux', '-u', 'attach', '-t', slug];
}

// One obvious way home: a detach key bound on the tmux server idempotently
// before each attach (C-b d always works too, for tmux hands). Default C-\ -
// the classic "escape the inner terminal" key (nvim terminal-mode lineage),
// untouched by the session programs. Override with WOLTSPACE_TUI_DETACH
// (any tmux key name: 'C-]', 'F9', ...).
export const detachKey = () => process.env.WOLTSPACE_TUI_DETACH || 'C-\\';

export const detachLabel = () =>
  detachKey().replace(/^C-/, 'ctrl-').replace(/^M-/, 'alt-');

function tmuxCmd(args) {
  return inContainer() ? ['tmux', ...args] : ['docker', 'exec', '-u', 'node', containerName(), 'tmux', ...args];
}

function ensureDetachKey() {
  for (const args of [
    ['unbind-key', '-n', 'F12'], // retired earlier default
    ['bind-key', '-n', detachKey(), 'detach-client'],
  ]) {
    const cmd = tmuxCmd(args);
    try {
      spawnSync(cmd[0], cmd.slice(1), { stdio: 'ignore' });
    } catch {
      /* non-fatal - C-b d always works */
    }
  }
}

export function attach(slug) {
  ensureDetachKey();
  const [cmd, ...args] = attachCommand(slug);
  const env = { ...process.env };
  delete env.TMUX; // allow attach from inside another tmux
  const r = spawnSync(cmd, args, { stdio: 'inherit', env });
  return r.status ?? 1;
}
