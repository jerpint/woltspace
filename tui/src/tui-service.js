#!/usr/bin/env node
// woltspace-tui-service - the pty bridge behind the browser terminal.
// Same exact identity as woltspace-tui; the Python launcher accepts it only on
// an exact name/version match, just like the TUI itself.

import { packageVersion, versionRecord } from './version.js';

const args = process.argv.slice(2);
if (args.includes('--version')) {
  console.log(args.includes('--json') ? JSON.stringify(versionRecord('woltspace-tui-service')) : packageVersion);
  process.exit(0);
}

const { startPtyService } = await import('./pty-service.js');
startPtyService({
  port: parseInt(process.env.TUI_PORT || '3001', 10),
  woltDir: process.env.WOLT_DIR || process.cwd(),
});
