// The session list as a tree: wolts are folders, their sessions are leaves.
//
// Pure functions over the lodge's /sessions rows, so the shape is testable
// without a terminal. Wolts with something running come first; wolts whose
// sessions all rest fold into one "resting" row at the bottom. Stopped and
// finished sessions show only with `showAll`, as the flat list did.

import { agentAlive } from './session-view.js';

export const isHeld = (s) => Boolean(s?.hold);
export const isResting = (s) => !agentAlive(s) && s?.status === 'resting';

// What the default view lists: a session you can talk to, or one that rests
// and wakes with its conversation on the next message.
export const isListed = (s, showAll) => showAll || agentAlive(s) || isResting(s);

export const sessionState = (s) => {
  if (agentAlive(s)) return isHeld(s) ? 'held' : 'running';
  if (isResting(s)) return 'resting';
  return 'stopped';
};

// The leaf label: the session's own title, else its name without the wolt
// prefix and the hex tail (`uxwolt-sleek-lodge-02a90d` -> `sleek-lodge`).
export const shortName = (s) => {
  const name = s?.name || '';
  const wolt = s?.wolt || '';
  const bare = wolt && name.startsWith(wolt + '-') ? name.slice(wolt.length + 1) : name;
  return bare.replace(/-[0-9a-f]{6}$/, '') || name;
};

export const sessionLabel = (s) => (s?.title ? s.title : shortName(s));

export function summarize(sessions) {
  const counts = { running: 0, resting: 0, held: 0 };
  for (const s of sessions || []) {
    if (agentAlive(s)) {
      counts.running++;
      if (isHeld(s)) counts.held++;
    } else if (isResting(s)) {
      counts.resting++;
    }
  }
  return counts;
}

const byRecent = (a, b) =>
  (agentAlive(b) - agentAlive(a)) ||
  (isResting(b) - isResting(a)) ||
  (b.last_activity || 0) - (a.last_activity || 0);

// Rows, top to bottom. `folded` is a Set of wolt names; `restingOpen` unfolds
// the resting group. `expandAll` ignores both (a search must find what it
// matches).
export function buildTree(sessions, { showAll = false, folded = new Set(), restingOpen = false, expandAll = false } = {}) {
  const groups = new Map();
  for (const s of sessions || []) {
    if (!isListed(s, showAll)) continue;
    const wolt = s.wolt || s.wolt_id || '?';
    if (!groups.has(wolt)) groups.set(wolt, []);
    groups.get(wolt).push(s);
  }
  const awake = [];
  const asleep = [];
  for (const wolt of [...groups.keys()].sort()) {
    const list = groups.get(wolt).sort(byRecent);
    (list.some(agentAlive) ? awake : asleep).push({ wolt, list });
  }

  const rows = [];
  const pushWolt = (group, { last, nested }) => {
    const running = group.list.filter(agentAlive).length;
    const open = expandAll || !folded.has(group.wolt);
    rows.push({
      kind: 'wolt', key: `w:${group.wolt}`, wolt: group.wolt, creature: group.list[0]?.creature,
      running, total: group.list.length, open, last, nested,
    });
    if (!open) return;
    group.list.forEach((s, i) => rows.push({
      kind: 'session', key: `s:${s.name}`, session: s, wolt: group.wolt,
      last: i === group.list.length - 1, parentLast: last, nested,
    }));
  };

  const topCount = awake.length + (asleep.length ? 1 : 0);
  awake.forEach((group, i) => pushWolt(group, { last: i === topCount - 1, nested: false }));
  if (asleep.length) {
    const open = expandAll || restingOpen;
    rows.push({
      kind: 'resting', key: 'resting', wolts: asleep.length,
      total: asleep.reduce((n, g) => n + g.list.length, 0), open, last: true,
    });
    if (open) asleep.forEach((group, i) => pushWolt(group, { last: i === asleep.length - 1, nested: true }));
  }
  return rows;
}

// The guide drawn in front of a row, like the `tree` command.
export function guide(row) {
  const branch = row.last ? '└─ ' : '├─ ';
  if (row.kind === 'session') {
    const outer = row.nested ? '   ' : '';
    return outer + (row.parentLast ? '   ' : '│  ') + branch;
  }
  if (row.kind === 'wolt' && row.nested) return '   ' + branch;
  return branch;
}
