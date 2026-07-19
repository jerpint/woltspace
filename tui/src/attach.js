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

export function attach(slug) {
  const [cmd, ...args] = attachCommand(slug);
  const env = { ...process.env };
  delete env.TMUX; // allow attach from inside another tmux
  const r = spawnSync(cmd, args, { stdio: 'inherit', env });
  return r.status ?? 1;
}
