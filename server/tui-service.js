/**
 * Minimal Node.js TUI service — just pty + WebSocket.
 *
 * The Python server proxies /tui WebSocket connections here.
 * This is the only piece that needs Node (node-pty + ws).
 *
 * Usage: TUI_PORT=3001 WOLT_DIR=/path node tui-service.js
 */

import { createServer } from 'node:http';
import { createRequire } from 'node:module';
import { execSync } from 'node:child_process';

const require = createRequire(import.meta.url);
const { WebSocketServer } = require('ws');
const pty = require('node-pty');

const PORT = parseInt(process.env.TUI_PORT || '3001');
const WOLT_DIR = process.env.WOLT_DIR || '/workspace';

function ensureTmuxSession(name = 'main') {
  const safe = name.replace(/[^a-zA-Z0-9_-]/g, '');
  try {
    execSync(`tmux has-session -t ${safe} 2>/dev/null`);
  } catch {
    execSync(`tmux new-session -d -s ${safe} -c ${WOLT_DIR}`);
  }
  return safe;
}

const server = createServer((req, res) => {
  if (req.url === '/health') {
    res.writeHead(200);
    res.end('ok');
    return;
  }
  res.writeHead(404);
  res.end();
});

const wss = new WebSocketServer({ server });

wss.on('connection', (ws, req) => {
  const url = new URL(req.url, `http://localhost:${PORT}`);
  const sessionName = ensureTmuxSession(url.searchParams.get('session') || 'main');
  console.log(`[tui] client → ${sessionName}`);

  const shell = pty.spawn('tmux', ['attach', '-t', sessionName], {
    name: 'xterm-256color',
    cols: 80,
    rows: 24,
    cwd: WOLT_DIR,
    env: { ...process.env, TERM: 'xterm-256color' },
  });

  shell.onData((data) => {
    try { ws.send(data); } catch {}
  });

  shell.onExit(() => {
    console.log(`[tui] pty exited (${sessionName})`);
    try { ws.close(); } catch {}
  });

  ws.on('message', (msg) => {
    const str = msg.toString();
    if (str[0] === '{') {
      try {
        const parsed = JSON.parse(str);
        if (parsed.type === 'resize' && parsed.cols && parsed.rows) {
          shell.resize(parsed.cols, parsed.rows);
          return;
        }
      } catch {}
    }
    shell.write(str);
  });

  ws.on('close', () => {
    console.log(`[tui] disconnected (${sessionName})`);
    shell.kill();
  });
});

server.listen(PORT, () => {
  console.log(`[tui-service] listening on port ${PORT}`);
});
