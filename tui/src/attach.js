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
  return inContainer()
    ? ['tmux', 'attach', '-t', slug]
    : ['docker', 'exec', '-it', '-u', 'node', containerName(), 'tmux', 'attach', '-t', slug];
}

export function attach(slug) {
  const [cmd, ...args] = attachCommand(slug);
  const env = { ...process.env };
  delete env.TMUX; // allow attach from inside another tmux
  const r = spawnSync(cmd, args, { stdio: 'inherit', env });
  return r.status ?? 1;
}
