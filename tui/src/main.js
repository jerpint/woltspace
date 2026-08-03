#!/usr/bin/env node
// woltspace tui - terminal cockpit for the colony.
// Runs on node >= 18 or bun; host or in-container (auto-detected).

import React from 'react';
import { render } from 'ink';
import App from './ui/App.js';
import { attach, inContainer, containerName } from './attach.js';
import { BASE, resumeSession, spawnSession } from './api.js';
import { lore } from './theme.js';

async function main() {
  for (;;) {
    let action = null;
    const instance = render(React.createElement(App, { onAction: (a) => { action = a; } }));
    await instance.waitUntilExit();
    if (!action || action.type === 'quit') break;

    // Resolve resume/spawn to a live tmux session, then attach to it.
    // API calls happen out here (ink unmounted) so failures print plainly
    // and the loop falls back to a fresh list either way.
    let slug = action.slug;
    try {
      if (action.type === 'resume') {
        console.error(lore.waking(slug));
        await resumeSession(slug);
      } else if (action.type === 'spawn') {
        console.error(lore.waking(action.wolt));
        const r = await spawnSession(action.wolt);
        slug = r?.name;
        if (!slug) throw new Error('lodge returned no session name');
      }
    } catch (e) {
      console.error(lore.wakeFailed(slug || action.wolt, e.message));
      continue;
    }
    const status = attach(slug);
    if (status !== 0) {
      const where = inContainer() ? 'in-container tmux' : `docker exec into '${containerName()}'`;
      console.error(`attach to ${slug} exited ${status} (${where}; lodge at ${BASE})`);
    }
    continue; // back to the list, fresh fetch
  }
}

main();
