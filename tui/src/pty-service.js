// The pty bridge behind the browser terminal.
//
// The Python server proxies every /tui websocket here; this attaches the
// session's tmux through node-pty and streams bytes both ways. It is the only
// piece of woltspace that needs Node at runtime (node-pty + ws), which is why
// it ships inside @woltspace/tui and not the Python wheel.
//
// Started by the control plane as a supervised connector ("tui" in
// `woltspace status`). Standalone: TUI_PORT=3001 WOLT_DIR=/path woltspace-tui-service

import { createServer } from 'node:http';
import { createRequire } from 'node:module';
import { execSync } from 'node:child_process';
import { chmodSync, statSync } from 'node:fs';
import { dirname, join } from 'node:path';

const require = createRequire(import.meta.url);

// node-pty 1.1.0's npm tarball ships prebuilds/darwin-*/spawn-helper as 0644
// and its postinstall only fixes build/Release/. The first pty.spawn on a Mac
// then dies with "posix_spawnp failed." Heal it here, once, before listening.
export function ensureSpawnHelperExecutable({ log = console.log, platform = process.platform, arch = process.arch } = {}) {
  if (platform !== 'darwin') return false;
  let helper;
  try {
    helper = join(dirname(require.resolve('node-pty/package.json')), 'prebuilds', `${platform}-${arch}`, 'spawn-helper');
    const { mode } = statSync(helper);
    if (mode & 0o111) return false;
    chmodSync(helper, (mode & 0o777) | 0o755);
  } catch {
    return false;
  }
  log(`[tui-service] made ${helper} executable (node-pty ships it without the bit)`);
  return true;
}

export function startPtyService({ port = 3001, woltDir = process.cwd(), log = console.log } = {}) {
  const { WebSocketServer } = require('ws');
  const pty = require('node-pty');
  ensureSpawnHelperExecutable({ log });

  function ensureTmuxSession(name = 'main') {
    const safe = name.replace(/[^a-zA-Z0-9_-]/g, '');
    try {
      execSync(`tmux has-session -t ${safe} 2>/dev/null`);
    } catch {
      if (safe === 'main') {
        // Only auto-create the main session — other sessions are managed by
        // start_session/resume_session via the API. Creating bare shells here
        // races with auto-resume and produces shells with wrong working dir.
        execSync(`tmux new-session -d -s ${safe} -c ${woltDir}`);
      } else {
        return null;
      }
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
    const url = new URL(req.url, `http://localhost:${port}`);
    const sessionName = ensureTmuxSession(url.searchParams.get('session') || 'main');
    if (!sessionName) {
      log(`[tui] no tmux session for ${url.searchParams.get('session')} — waiting for resume`);
      // Session doesn't exist yet. Close the WS — split.html will auto-resume
      // and reconnect once the tmux session is created.
      ws.close();
      return;
    }
    log(`[tui] client → ${sessionName}`);

    let shell;
    try {
      shell = pty.spawn('tmux', ['attach', '-t', sessionName], {
        name: 'xterm-256color',
        cols: 80,
        rows: 24,
        cwd: woltDir,
        env: { ...process.env, TERM: 'xterm-256color' },
      });
    } catch (err) {
      // One bad attach must not take the bridge down with it — every other
      // pane in the browser would go dark for a session that was never theirs.
      log(`[tui] pty spawn failed (${sessionName}): ${err.message}`);
      try { ws.close(); } catch {}
      return;
    }

    shell.onData((data) => {
      try { ws.send(data); } catch {}
    });

    shell.onExit(() => {
      log(`[tui] pty exited (${sessionName})`);
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
      log(`[tui] disconnected (${sessionName})`);
      shell.kill();
    });
  });

  // Without this the 'error' event is unhandled, so a busy port becomes an
  // uncaught exception: a Node stack trace in connector-tui.log and a bridge
  // that just isn't there. The default port is the API port + 1, so a second
  // instance on :7778 lands on the default instance's bridge — say which port
  // and which knob, then exit nonzero so the supervisor can act on it.
  server.on('error', (error) => {
    if (error && error.code === 'EADDRINUSE') {
      log(`[tui-service] cannot bind port ${port}: address already in use — `
        + 'something else holds it (another woltspace instance?). '
        + 'Set WOLTSPACE_TUI_PORT, or channels.tui.port in the data root config, '
        + 'to give this instance its own pty port.');
    } else {
      log(`[tui-service] listen on port ${port} failed: ${error && error.message}`);
    }
    process.exit(1);
  });

  server.listen(port, () => {
    log(`[tui-service] listening on port ${port}`);
  });
  return server;
}
