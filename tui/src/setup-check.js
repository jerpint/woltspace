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

// The CLI's lodge voice (src/woltspace/lore.py): an emoji and an amber
// headline, the moss lore line tucked under it, teal for anything you can
// type or open. Colour only on a terminal that wants it.
const PALETTE = {
  amber: [217, 158, 66], moss: [127, 166, 80], teal: [63, 176, 160],
  terra: [192, 92, 74], bark: [138, 127, 109],
};
const INDENT = '  ';
const SUBINDENT = '     ';

export function wantsColor(stream = process.stderr, env = process.env) {
  return Boolean(stream.isTTY) && !('NO_COLOR' in env) && env.TERM !== 'dumb';
}

export function setupMessage({ base, has, cli, color = false }) {
  const paint = (name, text) => (color
    ? `\x1b[38;2;${PALETTE[name].join(';')}m${text}\x1b[0m` : text);
  const head = (emoji, text) => `${INDENT}${emoji} ${paint('amber', text)}`;
  const sub = (text, name = 'moss') => `${SUBINDENT}${paint(name, text)}`;
  if (cli) {
    return [
      '',
      head('🌙', `no lodge answers at ${base}`),
      sub('lodge closed for the night - woltspace is installed'),
      '',
      head('☀️', 'wake it up, then run this again:'),
      sub(`${cli} start`, 'teal'),
      '',
    ].join('\n');
  }
  const mark = (ok, name) => (ok ? `${paint('moss', '✓')} ${name}` : `${paint('terra', '✗')} ${paint('bark', name)}`);
  const agent = AGENTS.find((name) => has[name]);
  const checks = [
    mark(has.uv, 'uv'),
    mark(has.tmux, 'tmux'),
    mark(agent, agent || 'claude / codex / opencode'),
    mark(false, 'woltspace'),
  ].join('   ');
  return [
    '',
    head('💤', `no lodge answers at ${base}`),
    sub("the river's quiet - woltspace isn't installed yet"),
    '',
    `${SUBINDENT}${checks}`,
    '',
    head('🦫', 'easiest: paste this into Claude Code, Codex or opencode'),
    sub(AGENT_PROMPT, 'teal'),
    '',
    head('🪵', 'or bring Onboardie into your agent'),
    sub('npx skills add jerpint/woltspace', 'teal'),
    '',
    `${INDENT}🌲 ${paint('bark', 'docs')} ${paint('teal', README)}`,
    '',
  ].join('\n');
}

// True when a lodge answers. Otherwise prints the checklist and returns false.
export async function checkLodge(base, {
  fetchFn, run, pathVar, color = wantsColor(), write = (s) => process.stderr.write(s),
} = {}) {
  if (await lodgeAnswers(base, fetchFn)) return true;
  const has = Object.fromEntries(['uv', 'tmux', ...AGENTS].map((name) => [name, onPath(name, pathVar)]));
  write(setupMessage({ base, has, cli: uvWoltspace(run), color }));
  return false;
}
