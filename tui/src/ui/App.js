// Sessions pane - the session list as a tree. Vim hands, lore voice, zero timers.

import React, { useState, useEffect, useRef, useCallback } from 'react';
import os from 'node:os';
import { Box, Text, useApp, useInput } from 'ink';
import * as api from '../api.js';
import { color, creatureGlyph, lore, age, clock } from '../theme.js';
import { agentAlive, sessionPolicy, sessionWorkdir, spawnTarget } from '../session-view.js';
import {
  buildTree, guide, isListed, isResting, sessionLabel, sessionState, shortName, summarize,
} from '../tree.js';
import { createWoltAction, validateWoltName, woltTypes } from '../create-wolt.js';
import { versionBanner } from '../version.js';

const h = React.createElement;

const userName = () =>
  process.env.WOLTSPACE_USER || process.env.HUMAN_NAME || os.userInfo().username || 'human';

// The detail pane needs room; below this many columns the tree has the screen.
const DETAIL_MIN_COLUMNS = 100;

export default function App({ onAction, launchCwd = process.cwd() }) {
  const { exit } = useApp();
  const [sessions, setSessions] = useState([]);
  const [wolts, setWolts] = useState([]);
  const [capabilities, setCapabilities] = useState(null);
  const [versionNote, setVersionNote] = useState('');
  const [fetchedAt, setFetchedAt] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [flash, setFlash] = useState('');
  const [mode, setMode] = useState('normal');
  const [cursor, setCursor] = useState(0);
  const [showAll, setShowAll] = useState(false);
  const [query, setQuery] = useState('');
  const [committed, setCommitted] = useState('');
  const [input, setInput] = useState('');
  const [spawnCursor, setSpawnCursor] = useState(0);
  const [createName, setCreateName] = useState('');
  const [createTypeCursor, setCreateTypeCursor] = useState(0);
  const [folded, setFolded] = useState(() => new Set());
  const [restingOpen, setRestingOpen] = useState(false);
  const pendingG = useRef(false);

  const needle = (mode === 'search' ? query : committed).toLowerCase();
  const isMatch = (s) =>
    Boolean(needle) && Boolean(s) &&
    (s.name.toLowerCase().includes(needle) ||
      (s.title || '').toLowerCase().includes(needle) ||
      (s.wolt || '').toLowerCase().includes(needle) ||
      (s.harness || '').toLowerCase().includes(needle) ||
      (s.model || '').toLowerCase().includes(needle) ||
      sessionWorkdir(s).toLowerCase().includes(needle));
  // A search unfolds everything, so a match is never hidden in a fold.
  const view = buildTree(sessions, { showAll, folded, restingOpen, expandAll: Boolean(needle) });
  const hidden = showAll ? 0 : sessions.filter((s) => !isListed(s, false)).length;
  const row = view[Math.min(cursor, view.length - 1)] || null;
  const selected = row?.kind === 'session' ? row.session : null;
  const counts = summarize(sessions);

  const refetch = useCallback(async (keepSlug) => {
    setLoading(true);
    try {
      const list = await api.listSessions();
      setSessions(list);
      setFetchedAt(new Date());
      setError('');
      if (keepSlug) {
        const v = buildTree(list, { showAll, folded, restingOpen });
        const i = v.findIndex((r) => r.kind === 'session' && r.session.name === keepSlug);
        if (i >= 0) setCursor(i);
      }
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [showAll, folded, restingOpen]);

  useEffect(() => {
    refetch();
    api.listWolts().then(setWolts).catch(() => {});
    api.runtimeCapabilities().then(setCapabilities).catch(() => {});
    // Advisory only: a mismatched pair still runs, it just says so once.
    api.lodgeHealth()
      .then((health) => setVersionNote(versionBanner(health?.version) || ''))
      .catch(() => {});
  }, []);

  const act = async (fn, doneMsg) => {
    setFlash('');
    setLoading(true);
    try {
      const result = await fn();
      setFlash(typeof doneMsg === 'function' ? doneMsg(result) : doneMsg);
      await refetch(selected?.name);
    } catch (e) {
      setError(e.message);
      setLoading(false);
    }
  };

  const move = (d) => {
    if (!view.length) return;
    setCursor((c) => Math.max(0, Math.min(view.length - 1, c + d)));
  };

  const jumpMatch = (dir) => {
    if (!committed || !view.length) return;
    for (let step = 1; step <= view.length; step++) {
      const i = (cursor + dir * step + view.length * step) % view.length;
      if (view[i].kind === 'session' && isMatch(view[i].session)) return setCursor(i);
    }
  };

  useInput((ch, key) => {
    // --- text-entry modes -------------------------------------------------
    if (mode === 'create-name') {
      if (key.escape) {
        setCreateName('');
        setMode('normal');
      } else if (key.return) {
        const validation = validateWoltName(createName);
        if (validation) setError(validation);
        else {
          setError('');
          setMode('create-type');
        }
      } else if (key.backspace || key.delete) {
        setCreateName(createName.slice(0, -1));
      } else if (ch && !key.ctrl && !key.meta && !key.tab) {
        setCreateName((createName + ch).toLowerCase());
      }
      return;
    }

    if (mode === 'search' || mode === 'send') {
      const [val, setVal] = mode === 'search' ? [query, setQuery] : [input, setInput];
      if (key.escape) {
        setVal('');
        if (mode === 'search') setCommitted('');
        setMode('normal');
      } else if (key.return) {
        if (mode === 'search') {
          setCommitted(query);
          setMode('normal');
          if (query) {
            const v = buildTree(sessions, { showAll, expandAll: true });
            const i = v.findIndex((r) => r.kind === 'session' && isMatch(r.session));
            if (i >= 0) setCursor(i);
          }
        } else if (input.trim() && selected) {
          const text = input.trim();
          const slug = selected.name;
          setInput('');
          setMode('normal');
          act(() => api.sendMessage(slug, text, userName()), lore.sent(slug));
        }
      } else if (key.backspace || key.delete) {
        setVal(val.slice(0, -1));
      } else if (ch && !key.ctrl && !key.meta && !key.tab) {
        setVal(val + ch);
      }
      return;
    }

    if (mode === 'confirm') {
      if (ch === 'y' && selected) {
        setMode('normal');
        act(() => api.stopSession(selected.name), lore.stopped(selected.name));
      } else {
        setMode('normal');
      }
      return;
    }

    if (mode === 'spawn-confirm') {
      if (key.escape || ch === 'n') {
        setMode('spawn');
      } else if (ch === 'y' && wolts[spawnCursor]) {
        const target = spawnTarget(capabilities, wolts[spawnCursor], launchCwd);
        onAction?.({
          type: 'spawn',
          wolt: wolts[spawnCursor].name,
          workdir: target.workdir,
          executionPolicy: target.executionPolicy,
          isolation: capabilities?.isolation,
        });
        exit();
      }
      return;
    }

    if (mode === 'create-confirm') {
      if (key.escape || ch === 'n') {
        setMode('create-type');
      } else if (ch === 'y') {
        onAction?.(createWoltAction(
          createName, woltTypes[createTypeCursor], capabilities, launchCwd,
        ));
        exit();
      }
      return;
    }

    if (mode === 'create-type') {
      if (key.escape) return setMode('create-name');
      if (ch === 'j' || key.downArrow) {
        return setCreateTypeCursor((c) => Math.min(woltTypes.length - 1, c + 1));
      }
      if (ch === 'k' || key.upArrow) {
        return setCreateTypeCursor((c) => Math.max(0, c - 1));
      }
      if (key.return) {
        if (!capabilities) setError('runtime capabilities are not loaded; press r and retry');
        else setMode('create-confirm');
      }
      return;
    }

    if (mode === 'spawn') {
      if (key.escape) return setMode('normal');
      if (ch === 'j' || key.downArrow) return setSpawnCursor((c) => Math.min(wolts.length - 1, c + 1));
      if (ch === 'k' || key.upArrow) return setSpawnCursor((c) => Math.max(0, c - 1));
      if (key.return && wolts[spawnCursor]) {
        if (!capabilities) {
          setError('runtime capabilities are not loaded yet');
          return;
        }
        setMode('spawn-confirm');
      }
      return;
    }

    // --- normal mode ------------------------------------------------------
    // Fast typing / key-repeat delivers a chunk ("jjj") as one input string.
    if (ch && /^j+$/.test(ch) && ch.length > 1) return move(ch.length);
    if (ch && /^k+$/.test(ch) && ch.length > 1) return move(-ch.length);
    if (ch === 'gg') {
      pendingG.current = false;
      setCursor(0);
      return;
    }
    if (ch === 'g') {
      if (pendingG.current) {
        pendingG.current = false;
        setCursor(0);
      } else {
        pendingG.current = true;
      }
      return;
    }
    pendingG.current = false;

    if (ch === 'q') {
      onAction?.({ type: 'quit' });
      exit();
    } else if (ch === 'h' || key.leftArrow) fold(true);
    else if (ch === 'l' || key.rightArrow) fold(false);
    else if (ch === 'j' || key.downArrow) move(1);
    else if (ch === 'k' || key.upArrow) move(-1);
    else if (ch === 'G') setCursor(Math.max(0, view.length - 1));
    else if (key.ctrl && ch === 'd') move(Math.ceil(rowBudget() / 2));
    else if (key.ctrl && ch === 'u') move(-Math.ceil(rowBudget() / 2));
    else if (ch === '/') {
      setQuery('');
      setMode('search');
    } else if (key.tab) jumpMatch(key.shift ? -1 : 1);
    else if (ch === 'r') refetch(selected?.name);
    else if (ch === 'a') {
      setShowAll((v) => !v);
      setCursor(0);
    } else if (ch === 'n') {
      // The picker starts on the wolt you are on.
      const openPicker = (list) => {
        setSpawnCursor(Math.max(0, list.findIndex((w) => w.name === row?.wolt)));
        setMode('spawn');
      };
      if (wolts.length) openPicker(wolts);
      else {
        api.listWolts().then((w) => {
          setWolts(w);
          openPicker(w);
        }).catch((e) => setError(e.message));
      }
    } else if (ch === 'c') {
      setCreateName('');
      setCreateTypeCursor(0);
      setError('');
      setMode('create-name');
    } else if (ch === 's' && agentAlive(selected)) {
      setInput('');
      setMode('send');
    } else if (ch === 'x' && (agentAlive(selected) || selected?.tmux_alive === true)) {
      setMode('confirm');
    } else if ((key.return || ch === 'o') && row && row.kind !== 'session') {
      fold(row.open);
    } else if ((key.return || ch === 'o') && selected) {
      // An agent in there → straight into the pane. No agent → rouse it first
      // (the server rebuilds tmux if needed, clears out a husk that only holds
      // a login shell, and restarts the agent with its harness's resume
      // flavor). Keyed on the agent, not on tmux: a stale agentless tmux
      // session used to read as alive here, and enter attached to the shell.
      onAction?.({
        type: agentAlive(selected) ? 'attach' : 'resume',
        slug: selected.name,
        isolation: capabilities?.isolation,
      });
      exit();
    }
  });

  // Fold (true) or unfold (false) what the cursor is on. On a session, folding
  // closes its wolt and moves the cursor up onto it.
  function fold(close) {
    if (!row) return;
    if (row.kind === 'resting') return setRestingOpen(!close);
    const wolt = row.wolt;
    if (row.kind === 'session' && !close) return;
    setFolded((prev) => {
      const next = new Set(prev);
      if (close) next.add(wolt); else next.delete(wolt);
      return next;
    });
    if (row.kind === 'session') {
      const i = view.findIndex((r) => r.kind === 'wolt' && r.wolt === wolt);
      if (i >= 0) setCursor(i);
    }
  }

  // --- rendering ----------------------------------------------------------
  const columns = process.stdout.columns || 80;
  const wide = columns >= DETAIL_MIN_COLUMNS;
  const screen = ['spawn', 'spawn-confirm', 'create-name', 'create-type', 'create-confirm'].includes(mode);
  // Border, header, summary, gaps, flash and the two key lines.
  const rowBudget = () => Math.max(3, (process.stdout.rows || 24) - 10);
  const budget = rowBudget();
  const top = Math.max(0, Math.min(cursor - Math.floor(budget / 2), view.length - budget));
  const visible = view.slice(top, top + budget);
  const k = (keyName, label) => [
    h(Text, { key: keyName + 'k', color: color.amber }, keyName),
    h(Text, { key: keyName + 'l', color: color.dim }, ` ${label}`),
  ];
  const keyLine = (pairs, extra) => h(Text, { wrap: 'truncate-end' },
    ...pairs.flatMap(([a, b], i) => [i ? h(Text, { key: `s${i}`, color: color.dim }, ' · ') : null, ...k(a, b)]),
    extra ? h(Text, { color: color.dim }, ` · ${extra}`) : null);

  const header = h(Box, null,
    h(Text, { color: color.amber, bold: true }, 'woltspace tui'),
    h(Text, { color: color.dim }, ` ─ the lodge${showAll ? ' (all)' : ''}`),
    h(Box, { flexGrow: 1 }),
    h(Text, { color: color.dim }, loading ? lore.loading : fetchedAt ? `as of ${clock(fetchedAt)}` : ''),
  );
  const summary = h(Text, { wrap: 'truncate-end' },
    '🪵 ',
    h(Text, { color: color.green }, `${counts.running} running`),
    h(Text, { color: color.dim }, ' · '),
    `${counts.resting} resting`,
    h(Text, { color: color.dim }, ' · '),
    counts.held ? h(Text, { color: color.amber }, `${counts.held} held 📌`) : h(Text, { color: color.dim }, '0 held'),
  );

  const body = screen ? screenCard() : h(Box, { marginTop: 1, flexGrow: 1 },
    h(Box, { flexDirection: 'column', width: wide ? '50%' : '100%', paddingRight: wide ? 1 : 0 },
      ...(view.length
        ? visible.map((r, idx) => treeRow(r, top + idx === cursor))
        : [h(Text, { key: 'empty', color: color.dim, italic: true }, '  ' + emptyText())])),
    wide ? h(Box, {
      flexDirection: 'column', flexGrow: 1, paddingLeft: 1,
      borderStyle: 'single', borderColor: color.dim, borderTop: false, borderRight: false, borderBottom: false,
    }, ...detail(row)) : null,
  );

  return h(Box, { flexDirection: 'column', borderStyle: 'round', borderColor: color.green, paddingX: 1 },
    header,
    versionNote ? h(Text, { key: 'vn', color: color.amber }, versionNote) : null,
    summary,
    body,
    h(Box, { marginTop: 1, flexDirection: 'column' }, ...statusLines()),
  );

  function emptyText() {
    if (loading) return lore.loading;
    if (needle) return lore.emptyMatch;
    if (showAll) return lore.emptyAll;
    // Quiet is not the same as empty: say what is one keystroke away.
    return hidden ? `${lore.emptyAlive} — ${lore.hiddenHint(hidden)}` : lore.emptyAlive;
  }

  function treeRow(r, sel) {
    const mark = h(Text, { color: color.terra, bold: true }, sel ? '▸' : ' ');
    const lead = h(Text, { color: color.dim, dimColor: true }, guide(r));
    if (r.kind === 'wolt') {
      return h(Text, { key: r.key, wrap: 'truncate-end', inverse: false },
        mark, lead,
        h(Text, { color: color.dim }, r.open ? '▾ ' : '▸ '),
        creatureGlyph(r.creature || woltOf(r.wolt)?.type) + ' ',
        h(Text, { bold: true }, r.wolt),
        '  ',
        r.running ? h(Text, { color: color.green }, String(r.running)) : null,
        h(Text, { color: color.dim }, r.running ? `/${r.total}` : String(r.total)),
      );
    }
    if (r.kind === 'resting') {
      return h(Text, { key: r.key, wrap: 'truncate-end' }, mark, lead,
        h(Text, { color: color.dim }, `${r.open ? '▾' : '▸'} ${r.wolts} ${r.wolts === 1 ? 'wolt' : 'wolts'} resting · ${r.total} ${r.total === 1 ? 'session' : 'sessions'}`));
    }
    const s = r.session;
    const state = sessionState(s);
    const dot = {
      running: h(Text, { color: color.green }, '● '),
      held: h(Text, null, '📌 '),
      resting: h(Text, { color: color.dim }, '○ '),
      stopped: h(Text, { color: color.dim }, '· '),
    }[state];
    const match = isMatch(s);
    const label = s.title
      ? h(Text, { bold: sel, underline: match, color: match ? color.amber : state === 'running' || state === 'held' ? undefined : color.dim }, s.title)
      : h(Text, { bold: sel, underline: match, italic: true, color: match ? color.amber : color.dim }, shortName(s));
    const used = agentAlive(s) && typeof s.idle_seconds === 'number' ? `  ${ago(s.idle_seconds)}` : '';
    return h(Text, { key: r.key, wrap: 'truncate-end' }, mark, lead, dot, label, h(Text, { color: color.dim }, used));
  }

  function detail(r) {
    if (!r) return [];
    const line = (key, label, value, valueColor) => h(Box, { key },
      h(Box, { width: 9, flexShrink: 0 }, h(Text, { color: color.dim }, label)),
      h(Text, { color: valueColor, wrap: 'truncate-end' }, value));
    if (r.kind === 'resting') {
      return [
        h(Text, { key: 't', bold: true, color: color.dim }, `${r.wolts} ${r.wolts === 1 ? 'wolt' : 'wolts'} resting`),
        h(Text, { key: 'd', color: color.dim, wrap: 'wrap' }, 'Nothing of theirs is running. A session wakes with its conversation on the next message.'),
        h(Text, { key: 'a', color: color.dim }, r.open ? 'h fold' : 'l or enter unfold'),
      ];
    }
    if (r.kind === 'wolt') {
      const w = woltOf(r.wolt);
      return [
        h(Text, { key: 't', bold: true }, `${creatureGlyph(r.creature || w?.type)} ${r.wolt}`),
        w?.description ? h(Text, { key: 'd', color: color.dim, wrap: 'wrap' }, w.description) : null,
        h(Text, { key: 'gap' }, ' '),
        w ? line('e', 'engine', engineLabel(w) || '?', color.amber) : null,
        line('n', 'sessions', `${r.running} running · ${r.total - r.running} ${showAll ? 'other' : 'resting'}`),
        h(Text, { key: 'gap2' }, ' '),
        h(Text, { key: 'a', color: color.dim }, `${r.open ? 'h fold' : 'l unfold'} · n new session for ${r.wolt}`),
      ];
    }
    const s = r.session;
    const state = sessionState(s);
    const stateText = {
      running: ['● running', color.green],
      held: ['📌 held', color.amber],
      resting: ['○ resting', color.dim],
      stopped: [`· ${s.status || 'stopped'}`, color.dim],
    }[state];
    let note = '';
    if (state === 'held') {
      const until = s.hold?.until ? `until ${clock(new Date(s.hold.until * 1000)).slice(0, 5)}` : 'until released';
      note = `${until}: ${s.hold?.reason || 'no reason given'}`;
    } else if (state === 'running' && typeof s.closes_in_seconds === 'number') {
      note = `rests in ${ago(s.closes_in_seconds)} if unused`;
    } else if (state === 'resting') {
      note = 'wakes with its conversation on enter';
    }
    const used = agentAlive(s) && typeof s.idle_seconds === 'number'
      ? `${ago(s.idle_seconds)} ago`
      : s.last_activity ? `${age(s.last_activity)} ago` : '?';
    return [
      s.title
        ? h(Text, { key: 't', bold: true, wrap: 'wrap' }, s.title)
        : h(Text, { key: 't', italic: true, color: color.dim }, 'untitled'),
      s.summary ? h(Text, { key: 'sm', wrap: 'wrap' }, s.summary) : null,
      h(Text, { key: 'gap' }, ' '),
      line('st', 'state', stateText[0], stateText[1]),
      note ? line('nt', '', note, color.dim) : null,
      line('w', 'wolt', `${creatureGlyph(s.creature)} ${s.wolt || s.wolt_id || '?'}`),
      line('e', 'engine', engineLabel(s) || '?', color.amber),
      line('f', 'from', s.adapter || 'lodge'),
      line('u', 'used', used),
      line('p', 'policy', sessionPolicy(s)),
      line('d', 'folder', tilde(sessionWorkdir(s)) || '?'),
      line('n', 'session', s.name, color.dim),
      h(Text, { key: 'gap2' }, ' '),
      h(Text, { key: 'a', color: color.dim },
        `enter ${agentAlive(s) ? 'attach' : 'wake and attach'}${agentAlive(s) ? ' · s send · x stop' : ''}`),
    ];
  }

  // n and c take over the body with one card; esc comes back to the tree.
  function screenCard() {
    const lines = [];
    const opt = (key, sel, ...children) => h(Text, { key, bold: sel, wrap: 'truncate-end' },
      h(Text, { color: color.terra, bold: true }, sel ? '▸ ' : '  '), ...children);
    if (mode === 'spawn') {
      lines.push(h(Text, { key: 't' }, h(Text, { color: color.amber, bold: true }, lore.spawnTitle), h(Text, { color: color.dim }, ' · new session')));
      lines.push(h(Text, { key: 'g' }, ' '));
      const room = Math.max(3, (process.stdout.rows || 24) - 16);
      const wTop = Math.max(0, Math.min(spawnCursor - Math.floor(room / 2), wolts.length - room));
      wolts.slice(wTop, wTop + room).forEach((w, idx) => {
        const i = wTop + idx;
        lines.push(opt(w.name, i === spawnCursor, creatureGlyph(w.type) + ' ', w.name, h(Text, { color: color.dim }, `  ${engineLabel(w)}`)));
      });
      lines.push(h(Text, { key: 'k', color: color.dim }, ' '));
      lines.push(keyLine([['j/k', 'pick'], ['enter', 'next'], ['esc', 'back']]));
    } else if (mode === 'spawn-confirm' && wolts[spawnCursor]) {
      const wolt = wolts[spawnCursor];
      const target = spawnTarget(capabilities, wolt, launchCwd);
      lines.push(h(Text, { key: 't', color: color.amber, bold: true }, `start ${creatureGlyph(wolt.type)} ${wolt.name}?`));
      lines.push(h(Text, { key: 'g' }, ' '));
      lines.push(cardLine('c', 'cwd', tilde(target.displayWorkdir)));
      lines.push(cardLine('e', 'engine', engineLabel(wolt) || 'lodge default', color.amber));
      lines.push(cardLine('p', 'policy', target.executionPolicy || 'server default (uses Auto grants)'));
      lines.push(h(Text, { key: 'k', color: color.dim }, ' '));
      lines.push(keyLine([['y', 'start and attach'], ['n/esc', 'back']]));
    } else if (mode === 'create-name') {
      lines.push(h(Text, { key: 't' }, h(Text, { color: color.amber, bold: true }, 'a new wolt'), h(Text, { color: color.dim }, ' · 1 of 3')));
      lines.push(h(Text, { key: 'g' }, ' '));
      lines.push(h(Text, { key: 'l', color: color.dim }, 'name'));
      lines.push(h(Text, { key: 'v' }, `${createName}▏`));
      lines.push(h(Text, { key: 'k', color: color.dim }, ' '));
      lines.push(keyLine([['enter', 'next'], ['esc', 'back']]));
    } else if (mode === 'create-type') {
      lines.push(h(Text, { key: 't' }, h(Text, { color: color.amber, bold: true }, `choose ${createName}'s creature`), h(Text, { color: color.dim }, ' · 2 of 3')));
      lines.push(h(Text, { key: 'g' }, ' '));
      woltTypes.forEach((type, i) => lines.push(opt(type, i === createTypeCursor, `${creatureGlyph(type)} ${type}`)));
      lines.push(h(Text, { key: 'k', color: color.dim }, ' '));
      lines.push(keyLine([['j/k', 'pick'], ['enter', 'next'], ['esc', 'back']]));
    } else if (mode === 'create-confirm') {
      const target = spawnTarget(capabilities, null, launchCwd);
      const type = woltTypes[createTypeCursor];
      lines.push(h(Text, { key: 't' }, h(Text, { color: color.amber, bold: true }, `create ${creatureGlyph(type)} ${createName}?`), h(Text, { color: color.dim }, ' · 3 of 3')));
      lines.push(h(Text, { key: 'g' }, ' '));
      lines.push(cardLine('ty', 'type', type));
      lines.push(cardLine('c', 'cwd', tilde(target.displayWorkdir)));
      lines.push(cardLine('p', 'policy', target.executionPolicy || 'server default (uses Auto grants)'));
      lines.push(h(Text, { key: 'k', color: color.dim }, ' '));
      lines.push(keyLine([['y', 'create and start its first session'], ['n/esc', 'back']]));
    }
    return h(Box, { marginTop: 1, flexGrow: 1, justifyContent: 'center' },
      h(Box, {
        flexDirection: 'column', borderStyle: 'round', borderColor: color.amber, paddingX: 2,
        width: Math.min(64, Math.max(30, columns - 6)),
      }, ...lines));
  }

  function cardLine(key, label, value, valueColor) {
    return h(Box, { key },
      h(Box, { width: 8, flexShrink: 0 }, h(Text, { color: color.dim }, label)),
      h(Text, { color: valueColor, wrap: 'truncate-end' }, value));
  }

  function woltOf(name) {
    return wolts.find((w) => w.name === name) || null;
  }

  function statusLines() {
    const lines = [];
    if (mode === 'search') {
      lines.push(h(Text, { key: 'm' }, h(Text, { color: color.amber }, '/'), h(Text, null, query), h(Text, { color: color.dim }, '  (enter commit · esc clear)')));
    } else if (mode === 'send' && selected) {
      lines.push(h(Text, { key: 'm' }, h(Text, { color: color.amber }, lore.sendTitle(selected.name) + ' '), h(Text, null, input), h(Text, { color: color.dim }, '▏ (enter send · esc cancel)')));
    } else if (mode === 'confirm' && selected) {
      lines.push(h(Text, { key: 'm', color: color.terra }, lore.stopConfirm(selected.name)));
    } else if (error) {
      lines.push(h(Text, { key: 'e', color: color.terra }, error));
    } else {
      lines.push(h(Text, { key: 'f', color: color.green }, flash || ' '));
    }
    if (!screen) {
      lines.push(h(Box, { key: 'k1' }, keyLine([
        ['j/k', 'move'], ['h/l', 'fold'],
        ['o/enter', selected && !agentAlive(selected) ? 'wake' : 'open'],
        ['n', 'new session'], ['c', 'new wolt'],
      ])));
      lines.push(h(Box, { key: 'k2' }, keyLine([
        ['s', 'send'], ['x', 'stop'], ['/', 'find'], ['a', 'all'], ['r', 'refresh'], ['q', 'quit'],
      ], hidden ? `${hidden} stopped hidden` : 'quit claude to come back')));
    }
    return lines;
  }
}

function engineLabel(s) {
  const harness = s?.harness || '';
  // A pin often repeats its engine (`claude-opus-5-5` under claude).
  const model = (s?.model || '').replace(harness ? new RegExp(`^${harness}-`) : /^$/, '');
  if (harness && model) return `${harness}/${model}`;
  return harness || model;
}

// Seconds as the list shows them: 45s, 12m, 3h, 2d.
function ago(seconds) {
  return age(Date.now() / 1000 - seconds);
}

function tilde(dir) {
  const home = os.homedir();
  return dir && home && dir.startsWith(home) ? '~' + dir.slice(home.length) : dir;
}
