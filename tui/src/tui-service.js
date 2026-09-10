#!/usr/bin/env node
// woltspace-tui-service - the pty bridge behind the browser terminal.
// Same identity as woltspace-tui; the Python launcher accepts any version of
// it as long as it really is this bin of this package.

import { lodgeSatisfies, minLodgeVersion, packageVersion, versionRecord } from './version.js';

const args = process.argv.slice(2);
if (args.includes('--version')) {
  console.log(args.includes('--json') ? JSON.stringify(versionRecord('woltspace-tui-service')) : packageVersion);
  process.exit(0);
}

// The one version check between the halves, and it runs here. The supervisor
// stamps WOLTSPACE_VERSION on this child; an unstamped launch is a lodge from
// before the stamp existed, which is what the fallback names.
const lodgeVersion = process.env.WOLTSPACE_VERSION;
if (!lodgeSatisfies(lodgeVersion)) {
  console.error(
    `woltspace-tui-service ${packageVersion} needs woltspace >= ${minLodgeVersion}, ` +
      `lodge is ${lodgeVersion || 'older than 0.5.0'} — upgrade it with ` +
      `\`uv tool install 'woltspace[connectors]'\`, then restart the control plane.`,
  );
  process.exit(1);
}

const { startPtyService } = await import('./pty-service.js');
startPtyService({
  port: parseInt(process.env.TUI_PORT || '3001', 10),
  woltDir: process.env.WOLTSPACE_WOLT_DIR || process.env.WOLT_DIR || process.cwd(),
});
