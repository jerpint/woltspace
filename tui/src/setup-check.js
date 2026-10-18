// Before the tui opens: is there a lodge to talk to?
//
// `npx woltspace` is often the first thing someone runs, on a machine with no
// lodge at all. Opening the cockpit just to show "lodge unreachable" helps
// nobody, so when no lodge answers we print what is there, what is missing
// and where to go, then exit. The install itself is never done here: the
// README's "For agents" section is the one copy of those steps, and this only
// points at it.

import { accessSync, constants, statSync } from 'node:fs';
import { delimiter, join } from 'node:path';
import { spawnSync } from 'node:child_process';

export const README = 'https://github.com/jerpint/woltspace';
export const AGENT_PROMPT = 'Read github.com/jerpint/woltspace and help me get started with the install';
export const AGENTS = ['claude', 'codex', 'opencode'];

// Any HTTP answer at all means a lodge is there; the tui reports anything
// else itself. A connection failure gets one more try, as api.js does.
export async function lodgeAnswers(base, fetchFn = fetch) {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      await fetchFn(`${base}/health`, { signal: AbortSignal.timeout(3000) });
      return true;
    } catch {
      /* try once more, then report */
    }
  }
  return false;
}

function isExecutable(path) {
  try {
    if (!statSync(path).isFile()) return false;
    accessSync(path, constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

export function onPath(name, pathVar = process.env.PATH || '') {
  return pathVar.split(delimiter).filter(Boolean).some((dir) => isExecutable(join(dir, name)));
}

// The woltspace CLI that uv installed, by its absolute path. Never looked up
// on PATH: under `npx woltspace`, npm puts its own bin folder first, so a bare
// `woltspace` there is this npm launcher, not the lodge's CLI.
export function uvWoltspace(run = spawnSync) {
  let result;
  try {
    result = run('uv', ['tool', 'dir', '--bin'], { encoding: 'utf8' });
  } catch {
    return null;
  }
  const dir = result?.status === 0 ? String(result.stdout || '').trim() : '';
  if (!dir) return null;
  const cli = join(dir, 'woltspace');
  return isExecutable(cli) ? cli : null;
}

export function setupMessage({ base, has, cli }) {
  if (cli) {
    return [
      `woltspace is installed, but no lodge answers at ${base}.`,
      '',
      'Start it, then run this again:',
      `  ${cli} start`,
      '',
    ].join('\n');
  }
  const mark = (ok) => (ok ? '✓' : '✗');
  const agent = AGENTS.find((name) => has[name]);
  const checks = [
    `${mark(has.uv)} uv`,
    `${mark(has.tmux)} tmux`,
    `${mark(agent)} ${agent || 'claude / codex / opencode'}`,
    '✗ woltspace',
  ].join('   ');
  return [
    `No lodge answers at ${base}, and woltspace isn't installed yet.`,
    '',
    `  ${checks}`,
    '',
    'Easiest: paste this into Claude Code, Codex or opencode:',
    `  ${AGENT_PROMPT}`,
    '',
    'Or give your agent the install skill:  npx skills add jerpint/woltspace',
    `Docs: ${README}`,
    '',
  ].join('\n');
}

// True when a lodge answers. Otherwise prints the checklist and returns false.
export async function checkLodge(base, { fetchFn, run, pathVar, write = (s) => process.stderr.write(s) } = {}) {
  if (await lodgeAnswers(base, fetchFn)) return true;
  const has = Object.fromEntries(['uv', 'tmux', ...AGENTS].map((name) => [name, onPath(name, pathVar)]));
  write(setupMessage({ base, has, cli: uvWoltspace(run) }));
  return false;
}
