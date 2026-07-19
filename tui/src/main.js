#!/usr/bin/env node
// woltspace tui - terminal cockpit for the colony.
// Runs on node >= 18 or bun; host or in-container (auto-detected).

import React from 'react';
import { render } from 'ink';
import App from './ui/App.js';
import { attach, inContainer, containerName } from './attach.js';
import { BASE } from './api.js';

async function main() {
  for (;;) {
    let action = null;
    const instance = render(React.createElement(App, { onAction: (a) => { action = a; } }));
    await instance.waitUntilExit();
    if (action?.type === 'attach') {
      const status = attach(action.slug);
      if (status !== 0) {
        const where = inContainer() ? 'in-container tmux' : `docker exec into '${containerName()}'`;
        console.error(`attach to ${action.slug} exited ${status} (${where}; lodge at ${BASE})`);
      }
      continue; // back to the list, fresh fetch
    }
    break;
  }
}

main();
