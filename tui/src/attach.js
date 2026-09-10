// Attach to a session's real tmux. The tui suspends, the terminal belongs to
// the session until it ends, then the list re-renders with a fresh fetch.
//
// outside tmux:  tmux attach -t <slug> - the classic client.
// inside tmux:   the same client, nested, so the wolt renders IN THE PANE you
//                are in. The wolt's session stays where it is - telegram
//                delivery, the web tui and the vulture all address it by
//                session name - and this pane holds a viewer onto it.
// container:     same two modes. external host: same two modes over
//                `docker exec` into the lodge container.
//
// There is no woltspace key here, and the tui binds nothing on your tmux
// server. Once the wolt renders in a pane, your own tmux is the navigation:
//
//   done with it            quit claude - the session ends and the pane falls
//                           back to the list, since claude IS the session's
//                           process (spawned with no remain-on-exit)
//   look away, keep it      prefix c / prefix n, like any other pane
//   want the pane back      prefix & - that kills the viewer, never the wolt,
//                           which is a session of its own
//
// Earlier versions moved your whole client with switch-client and then bound a
// detach key on your server to undo it. Both are gone: the rebinding ate the
// root binding of the person who reported the teleport, and the interception
// that replaced it would have swallowed a key claude itself uses.

import { existsSync } from 'node:fs';
import { spawnSync, execSync } from 'node:child_process';

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

// A wolt shown inside someone's pane should read as the agent, not as a second
// tmux: its own status bar would otherwise paint at the foot of the pane, an
// inch above the user's. `status` is a session option, so this touches only the
// wolt's session (`slug:` = that session, never a prefix match). It is sticky -
// the bar stays off for later attaches, including classic ones - and that is
// the same barless view the web tui already gives that session.
export const statusOffCommand = (slug, options = {}) =>
  tmuxCmd(['set-option', '-t', `${slug}:`, 'status', 'off'], options);

// Runs with the same TMUX-free environment as the client, and for the same
// reason: with $TMUX still set, tmux talks to the server the USER is sitting
// in, not the one the wolt lives on. On the socket the tui was launched from
// the slug is simply not a session (the bar stays on, silently); on a server
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

export function attach(slug, options = {}) {
  const [cmd, ...args] = attachCommand(slug, options);
  const env = { ...process.env };
  // Nesting is the point when we are inside tmux, and outside it this is a
  // no-op - either way tmux's own "sessions should be nested with care" guard
  // has nothing to refuse.
  delete env.TMUX;

  // Only when nesting: standing alone, a wolt should keep the bar it came with.
  if (insideTmux()) quietStatus(slug, options, env);

  // Inherited stdio, so the client owns the terminal directly: it sees SIGWINCH
  // on a resize, restores the screen it borrowed when it leaves, and speaks
  // mouse reporting to the real terminal. Nothing of ours sits in the middle,
  // which is also why every key reaches the outer tmux first.
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
