// Sessions pane - the v1 surface. Vim hands, lore voice, zero timers.

import React, { useState, useEffect, useRef, useCallback } from 'react';
import os from 'node:os';
import { Box, Text, useApp, useInput } from 'ink';
import * as api from '../api.js';
import { detachLabel } from '../attach.js';
import { color, creatureGlyph, lore, age, clock } from '../theme.js';
import { sessionPolicy, sessionWorkdir, spawnTarget } from '../session-view.js';

const h = React.createElement;

const userName = () =>
  process.env.WOLTSPACE_USER || process.env.HUMAN_NAME || os.userInfo().username || 'human';

const sortSessions = (list) =>
  [...list].sort((a, b) => (b.alive - a.alive) || (b.last_activity || 0) - (a.last_activity || 0));

export default function App({ onAction, launchCwd = process.cwd() }) {
  const { exit } = useApp();
  const [sessions, setSessions] = useState([]);
  const [wolts, setWolts] = useState([]);
  const [capabilities, setCapabilities] = useState(null);
  const [fetchedAt, setFetchedAt] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [flash, setFlash] = useState('');
  const [mode, setMode] = useState('normal'); // normal | search | send | spawn | spawn-confirm | confirm
  const [cursor, setCursor] = useState(0);
  const [showAll, setShowAll] = useState(false);
  const [query, setQuery] = useState('');
  const [committed, setCommitted] = useState('');
  const [input, setInput] = useState('');
  const [spawnCursor, setSpawnCursor] = useState(0);
  const pendingG = useRef(false);

  const view = sortSessions(sessions).filter((s) => showAll || s.alive);
  const selected = view[Math.min(cursor, view.length - 1)] || null;
  const needle = (mode === 'search' ? query : committed).toLowerCase();
  const isMatch = (s) =>
    needle &&
    (s.name.toLowerCase().includes(needle) ||
      (s.wolt || '').toLowerCase().includes(needle) ||
      (s.harness || '').toLowerCase().includes(needle) ||
      (s.model || '').toLowerCase().includes(needle) ||
      sessionWorkdir(s).toLowerCase().includes(needle));

  const refetch = useCallback(async (keepSlug) => {
    setLoading(true);
    try {
      const list = await api.listSessions();
      setSessions(list);
      setFetchedAt(new Date());
      setError('');
      if (keepSlug) {
        const v = sortSessions(list).filter((s) => showAll || s.alive);
        const i = v.findIndex((s) => s.name === keepSlug);
        if (i >= 0) setCursor(i);
      }
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [showAll]);

  useEffect(() => {
    refetch();
    api.listWolts().then(setWolts).catch(() => {});
    api.runtimeCapabilities().then(setCapabilities).catch(() => {});
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
      if (isMatch(view[i])) return setCursor(i);
    }
  };

  useInput((ch, key) => {
    // --- text-entry modes -------------------------------------------------
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
            const i = view.findIndex((s) =>
              s.name.toLowerCase().includes(query.toLowerCase()) ||
              (s.wolt || '').toLowerCase().includes(query.toLowerCase()));
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
        });
        exit();
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
    } else if (ch === 'j' || key.downArrow) move(1);
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
      if (wolts.length) {
        setSpawnCursor(0);
        setMode('spawn');
      } else {
        api.listWolts().then((w) => {
          setWolts(w);
          setSpawnCursor(0);
          setMode('spawn');
        }).catch((e) => setError(e.message));
      }
    } else if (ch === 's' && selected?.alive) {
      setInput('');
      setMode('send');
    } else if (ch === 'x' && selected?.alive) {
      setMode('confirm');
    } else if (key.return && selected) {
      // Alive → straight into the pane. Offline → rouse it first (the server
      // rebuilds tmux + restarts the agent with its harness's resume flavor).
      onAction?.({ type: selected.alive ? 'attach' : 'resume', slug: selected.name });
      exit();
    }
  });

  // --- rendering ----------------------------------------------------------
  const rowBudget = () => Math.max(2, Math.floor(((process.stdout.rows || 24) - 10) / 2));
  const budget = rowBudget();
  const top = Math.max(0, Math.min(cursor - Math.floor(budget / 2), view.length - budget));
  const visible = view.slice(top, top + budget);
  const slugWidth = Math.max(12, ...view.map((s) => s.name.length));
  const engineWidth = Math.max(8, ...view.map((s) => engineLabel(s).length));

  const rows = visible.map((s, idx) => {
    const i = top + idx;
    const sel = i === cursor;
    const match = isMatch(s);
    return h(Box, { key: s.name, flexDirection: 'column' },
      h(Box, null,
        h(Text, { color: color.terra, bold: true }, sel ? '▸ ' : '  '),
        h(Text, { color: s.alive ? color.green : color.dim }, s.alive ? '● ' : '○ '),
        h(Text, null, creatureGlyph(s.creature) + ' '),
        h(Text, {
          bold: sel,
          underline: match,
          color: match ? color.amber : undefined,
        }, s.name.padEnd(slugWidth + 2)),
        h(Text, { color: color.amber, dimColor: !sel }, engineLabel(s).padEnd(engineWidth + 2)),
        h(Text, { color: color.dim }, age(s.last_activity).padStart(4)),
      ),
      h(Text, { color: color.dim },
        `     ${s.wolt_id || s.wolt || '?'} · ${sessionPolicy(s)} · ${sessionWorkdir(s) || '?'}`),
    );
  });

  const list = view.length
    ? rows
    : [h(Text, { key: 'empty', color: color.dim, italic: true },
        '  ' + (loading ? lore.loading : needle ? lore.emptyMatch : showAll ? lore.emptyAll : lore.emptyAlive))];

  return h(Box, { flexDirection: 'column', borderStyle: 'round', borderColor: color.green, paddingX: 1 },
    h(Box, null,
      h(Text, { color: color.amber, bold: true }, 'woltspace tui'),
      h(Text, { color: color.dim }, ` ─ sessions${showAll ? ' (all)' : ''}`),
      h(Box, { flexGrow: 1 }),
      h(Text, { color: color.dim }, loading ? lore.loading : fetchedAt ? `as of ${clock(fetchedAt)}` : ''),
    ),
    h(Box, { flexDirection: 'column', marginTop: 1 }, ...list),
    h(Box, { marginTop: 1, flexDirection: 'column' }, ...statusLines()),
  );

  function statusLines() {
    const lines = [];
    if (mode === 'search') {
      lines.push(h(Text, { key: 'm' }, h(Text, { color: color.amber }, '/'), h(Text, null, query), h(Text, { color: color.dim }, '  (enter commit · esc clear)')));
    } else if (mode === 'send' && selected) {
      lines.push(h(Text, { key: 'm' }, h(Text, { color: color.amber }, lore.sendTitle(selected.name) + ' '), h(Text, null, input), h(Text, { color: color.dim }, '▏ (enter send · esc cancel)')));
    } else if (mode === 'confirm' && selected) {
      lines.push(h(Text, { key: 'm', color: color.terra }, lore.stopConfirm(selected.name)));
    } else if (mode === 'spawn') {
      lines.push(h(Text, { key: 'st', color: color.amber }, lore.spawnTitle));
      const wTop = Math.max(0, Math.min(spawnCursor - 3, wolts.length - 7));
      wolts.slice(wTop, wTop + 7).forEach((w, idx) => {
        const i = wTop + idx;
        lines.push(h(Text, { key: w.name, bold: i === spawnCursor },
          (i === spawnCursor ? ' ▸ ' : '   ') + creatureGlyph(w.type) + ' ' + w.name));
      });
      lines.push(h(Text, { key: 'sh', color: color.dim }, '   j/k pick · enter wake · esc cancel'));
    } else if (mode === 'spawn-confirm' && wolts[spawnCursor]) {
      const wolt = wolts[spawnCursor];
      const target = spawnTarget(capabilities, wolt, launchCwd);
      lines.push(h(Text, { key: 'sc1', color: color.amber }, `start ${wolt.name}?`));
      lines.push(h(Text, { key: 'sc2' }, `   cwd: ${target.displayWorkdir}`));
      lines.push(h(Text, { key: 'sc3' }, `   policy: ${target.executionPolicy}`));
      lines.push(h(Text, { key: 'sc4', color: color.dim }, '   y confirm · n/esc back'));
    } else {
      lines.push(h(Text, { key: 'k1', color: color.dim },
        `j/k move  enter attach${selected && !selected.alive ? ' (wakes it)' : ''} (${detachLabel()} comes back)  n new  s send  x stop`));
      lines.push(h(Text, { key: 'k2', color: color.dim },
        'r refresh  / find  tab/shift-tab match  a all  q quit'));
    }
    if (error) lines.push(h(Text, { key: 'e', color: color.terra }, error));
    else if (flash) lines.push(h(Text, { key: 'f', color: color.green }, flash));
    return lines;
  }
}

function engineLabel(s) {
  return s.model ? `${s.harness}/${s.model}` : s.harness || '';
}
