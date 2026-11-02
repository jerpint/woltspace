#!/usr/bin/env node
// npx woltspace: open the woltspace tui. When no lodge answers, the tui
// prints what is missing and how to install, then exits.
//
// This is only a door. Woltspace itself installs with uv (see the README),
// and its CLI is a different program that may share this name on PATH, so
// commands such as `start` are never forwarded from here.

import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { pathToFileURL } from 'node:url';

const command = process.argv.slice(2).find((arg) => !arg.startsWith('-'));
if (command) {
  console.error(
    `npx woltspace only opens the tui. "${command}" belongs to the woltspace CLI,\n`
    + 'which installs with uv: https://github.com/jerpint/woltspace',
  );
  process.exit(2);
}

// The tui's own bin, by name: its package has two, so npm can't pick one.
const require = createRequire(import.meta.url);
const manifestPath = require.resolve('@woltspace/tui/package.json');
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
await import(pathToFileURL(join(dirname(manifestPath), manifest.bin['woltspace-tui'])).href);
