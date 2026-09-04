#!/usr/bin/env node
// woltspace tui - terminal cockpit for the colony.
// Runs on node >= 18 or bun; host or in-container (auto-detected).

import { packageVersion, versionRecord } from './version.js';

const args = process.argv.slice(2);
if (args.includes('--version')) {
  console.log(args.includes('--json') ? JSON.stringify(versionRecord()) : packageVersion);
  process.exit(0);
}

async function main() {
  const { default: React } = await import('react');
  const { render } = await import('ink');
  const { default: App } = await import('./ui/App.js');
  const { attach, inContainer, containerName } = await import('./attach.js');
  const { BASE, resumeSession, spawnSession } = await import('./api.js');
  const { lore } = await import('./theme.js');
  const launchCwd = process.cwd();
  for (;;) {
    let action = null;
    const instance = render(React.createElement(App, {
      launchCwd,
      onAction: (a) => { action = a; },
    }));
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
        const r = await spawnSession(action.wolt, action.workdir, action.executionPolicy);
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
