import { createServer } from 'node:http';
import { readFile, writeFile, readdir, mkdir } from 'node:fs/promises';
import { join, extname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { existsSync, statSync, readFileSync, readdirSync, writeFileSync, appendFileSync, mkdirSync, watch } from 'node:fs';
import { execSync, spawn } from 'node:child_process';
import { createConnection } from 'node:net';
import { request as httpRequest } from 'node:http';

// Optional TUI deps — only available inside the Docker container
// Use createRequire instead of dynamic import() because NODE_PATH
// is ignored by ESM resolution but respected by CommonJS require()
let WebSocketServer, pty;
try {
  const require = createRequire(import.meta.url);
  WebSocketServer = require('ws').WebSocketServer;
  pty = require('node-pty');
} catch {
  console.log('[tui] ws/node-pty not available — /tui disabled (normal outside Docker)');
}

const __dirname = fileURLToPath(new URL('.', import.meta.url));
const WOLT_DIR = process.env.WOLT_DIR || __dirname;
const SITE_DIR = join(WOLT_DIR, 'wolt', 'site');
const SPARKS_DIR = join(WOLT_DIR, 'wolt', 'sparks');
const PUBLIC_DIR = join(__dirname, 'public');  // platform UI assets (baked into image)
const STATE_DIR = join(WOLT_DIR, '.state');
const WOLT_NAME = process.env.WOLT_NAME || 'wolt';
const PORT = 3000;

// --- State dir (session data, tool registry, cron flags) ---

const TOOL_REGISTRY_FILE = join(STATE_DIR, 'tool-registry.json');
const CURRENT_URL_FILE   = join(STATE_DIR, 'current-url.json');
const VIEWS_HISTORY_FILE = join(STATE_DIR, 'views-history.jsonl');
const STATUS_FILE        = join(STATE_DIR, 'status.json');

async function ensureStateDir() {
  await mkdir(STATE_DIR, { recursive: true });
}

// --- Current view (right pane of split) ---

function getCurrentUrl() {
  if (!existsSync(CURRENT_URL_FILE)) return '/index.html';
  try { return JSON.parse(readFileSync(CURRENT_URL_FILE, 'utf8')).url || '/index.html'; } catch { return '/index.html'; }
}

function setCurrentUrl(url) {
  mkdirSync(STATE_DIR, { recursive: true });
  writeFileSync(CURRENT_URL_FILE, JSON.stringify({ url, updated: Date.now() }));
  console.log(`[current] → ${url}`);
}

function deriveTitleForUrl(u) {
  if (u === '/' || u === '/index.html') return 'home';
  if (u.startsWith('/history/')) {
    const id = u.slice('/history/'.length);
    try { return JSON.parse(readFileSync(join(SPARKS_DIR, id + '.json'), 'utf8')).title || id; }
    catch { return id; }
  }
  return u.split('/').pop().replace('.html', '').replace(/-/g, ' ') || u;
}

function logView(u, title) {
  try {
    mkdirSync(STATE_DIR, { recursive: true });
    appendFileSync(VIEWS_HISTORY_FILE, JSON.stringify({ url: u, title: title || deriveTitleForUrl(u), t: Date.now() }) + '\n');
  } catch {}
}

function readViewsHistory(n = 100) {
  if (!existsSync(VIEWS_HISTORY_FILE)) return [];
  try {
    return readFileSync(VIEWS_HISTORY_FILE, 'utf8')
      .trim().split('\n').filter(Boolean)
      .map(l => { try { return JSON.parse(l); } catch { return null; } })
      .filter(Boolean)
      .slice(-n)
      .reverse();
  } catch { return []; }
}

// --- Tool proxy registry ---

const toolRegistry = new Map();

function saveToolRegistry() {
  try {
    mkdirSync(STATE_DIR, { recursive: true });
    const data = {};
    for (const [name, info] of toolRegistry) data[name] = info;
    writeFileSync(TOOL_REGISTRY_FILE, JSON.stringify(data));
  } catch (e) {
    console.error('[tools] save failed:', e.message);
  }
}

function registerTool(name, port, pid, command) {
  toolRegistry.set(name, { port, pid, command, startedAt: Date.now() });
  saveToolRegistry();
  console.log(`[tools] registered ${name} on port ${port} (pid ${pid})`);
}

function unregisterTool(name) {
  const tool = toolRegistry.get(name);
  if (tool) {
    try { process.kill(tool.pid); } catch {}
    toolRegistry.delete(name);
    saveToolRegistry();
    console.log(`[tools] unregistered ${name}`);
  }
}

async function restoreToolRegistry() {
  await ensureStateDir();
  if (!existsSync(TOOL_REGISTRY_FILE)) return;
  try {
    const data = JSON.parse(readFileSync(TOOL_REGISTRY_FILE, 'utf8'));
    for (const [name, info] of Object.entries(data)) {
      const alive = (() => { try { process.kill(info.pid, 0); return true; } catch { return false; } })();
      if (alive) {
        toolRegistry.set(name, info);
        console.log(`[tools] restored ${name} (pid ${info.pid} still alive)`);
      } else if (info.command) {
        const child = spawn('sh', ['-c', info.command], {
          cwd: WOLT_DIR, env: { ...process.env, PORT: String(info.port) },
          stdio: 'pipe', detached: true,
        });
        child.unref();
        child.on('exit', () => unregisterTool(name));
        toolRegistry.set(name, { ...info, pid: child.pid, startedAt: Date.now() });
        console.log(`[tools] respawned ${name} on port ${info.port} (new pid ${child.pid})`);
      }
    }
    saveToolRegistry();
  } catch (e) { console.error('[tools] restore failed:', e.message); }
}

restoreToolRegistry();

setInterval(() => {
  for (const [name, tool] of toolRegistry) {
    try { process.kill(tool.pid, 0); } catch { toolRegistry.delete(name); saveToolRegistry(); }
  }
}, 30000);

// --- Spark/digest storage ---

async function listSparks() {
  try {
    const files = await readdir(SPARKS_DIR);
    const sparks = [];
    for (const f of files.filter(f => f.endsWith('.json'))) {
      try {
        const raw = await readFile(join(SPARKS_DIR, f), 'utf8');
        const { id, type, title, timestamp, parentId } = JSON.parse(raw);
        sparks.push({ id, type, title, timestamp, parentId: parentId || null });
      } catch {}
    }
    return sparks.sort((a, b) => b.timestamp.localeCompare(a.timestamp));
  } catch { return []; }
}

async function getSpark(id) {
  const raw = await readFile(join(SPARKS_DIR, `${id}.json`), 'utf8');
  return JSON.parse(raw);
}

async function getSparkWithChain(id) {
  const spark = await getSpark(id);
  const allSparks = await listSparks();
  const children = allSparks.filter(s => s.parentId === id);
  let chain = [id];
  let current = spark;
  while (current.parentId) {
    chain.unshift(current.parentId);
    try { current = await getSpark(current.parentId); } catch { break; }
  }
  let nextId = children.length > 0 ? children[0].id : null;
  let walkId = nextId;
  while (walkId) {
    chain.push(walkId);
    const nextChildren = allSparks.filter(s => s.parentId === walkId);
    walkId = nextChildren.length > 0 ? nextChildren[0].id : null;
  }
  const versionIndex = chain.indexOf(id);
  return {
    id: spark.id, type: spark.type, title: spark.title, timestamp: spark.timestamp,
    parentId: spark.parentId || null,
    childId: children.length > 0 ? children[0].id : null,
    version: versionIndex + 1, totalVersions: chain.length, html: spark.html,
  };
}

const MIME = {
  '.html': 'text/html', '.css': 'text/css', '.js': 'text/javascript',
  '.json': 'application/json', '.svg': 'image/svg+xml', '.xml': 'application/xml',
  '.txt': 'text/plain', '.pub': 'text/plain',
};

// --- TUI HTML ---

function tuiHtml(sessionName = 'main') {
  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>tui · ${sessionName}</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/css/xterm.min.css">
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    html, body { height: 100dvh; overflow: hidden; background: #0d1117; overscroll-behavior: none; }
    body { display: flex; flex-direction: column; font-family: 'SF Mono','Fira Code','Consolas',monospace; }
    #terminal { touch-action: none; }
    #topbar {
      background: #161b22; border-bottom: 1px solid #21262d;
      padding: 0.4rem 0.75rem; display: flex; align-items: center;
      justify-content: space-between; font-size: 0.8rem; flex-shrink: 0;
    }
    #topbar .title { color: #6b9; font-weight: 600; }
    #topbar .status { color: #555; font-size: 0.7rem; }
    #terminal { flex: 1; overflow: hidden; touch-action: none; }
    .xterm { height: 100%; touch-action: none; }
  </style>
</head>
<body>
  <div id="topbar">
    <div>
      <span class="title">tui · ${sessionName}</span>
      <span class="status" id="status">connecting...</span>
    </div>
  </div>
  <div id="terminal"></div>

  <script type="module">
    import { Terminal } from 'https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/+esm';
    import { FitAddon } from 'https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.10.0/+esm';
    import { WebLinksAddon } from 'https://cdn.jsdelivr.net/npm/@xterm/addon-web-links@0.11.0/+esm';

    const statusEl = document.getElementById('status');
    const term = new Terminal({
      cursorBlink: true,
      fontSize: 14,
      fontFamily: "'SF Mono','Fira Code','Consolas',monospace",
      theme: {
        background: '#0d1117',
        foreground: '#c9d1d9',
        cursor: '#6b9',
        selectionBackground: '#264f78',
        black: '#0d1117',
        red: '#f66',
        green: '#6b9',
        yellow: '#e5c07b',
        blue: '#61afef',
        magenta: '#c678dd',
        cyan: '#56b6c2',
        white: '#c9d1d9',
      },
    });

    const fitAddon = new FitAddon();
    term.loadAddon(fitAddon);
    term.loadAddon(new WebLinksAddon());
    term.open(document.getElementById('terminal'));
    fitAddon.fit();

    let ws = null;
    let reconnectTimer = null;

    function connect() {
      const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      const sessionParam = new URLSearchParams(location.search).get('session') || '${sessionName}';
      ws = new WebSocket(proto + '//' + location.host + '/tui?session=' + encodeURIComponent(sessionParam));

      ws.onopen = () => {
        statusEl.textContent = 'connected';
        statusEl.style.color = '#6b9';
        ws.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }));
      };

      ws.onmessage = (ev) => {
        term.write(ev.data);
      };

      ws.onclose = () => {
        statusEl.textContent = 'disconnected — reconnecting...';
        statusEl.style.color = '#f66';
        reconnectTimer = setTimeout(connect, 2000);
      };

      ws.onerror = () => {
        ws.close();
      };
    }

    term.onData((data) => {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(data);
      }
    });

    term.onResize(({ cols, rows }) => {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'resize', cols, rows }));
      }
    });

    window.addEventListener('resize', () => fitAddon.fit());
    new ResizeObserver(() => fitAddon.fit()).observe(document.getElementById('terminal'));

    // Scroll: enter tmux copy mode + arrow keys (no tmux mouse mode needed)
    // This keeps text selection working normally in the browser
    let inCopyMode = false;
    let copyModeTimer = null;
    const termEl = document.getElementById('terminal');

    termEl.addEventListener('wheel', (e) => {
      e.preventDefault();
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      const lines = Math.max(1, Math.round(Math.abs(e.deltaY) / 40));
      if (!inCopyMode) {
        ws.send('\\x02[');  // Ctrl-b [ = enter tmux copy mode
        inCopyMode = true;
      }
      const arrow = e.deltaY < 0 ? '\\x1b[A' : '\\x1b[B';
      for (let i = 0; i < lines; i++) ws.send(arrow);
      clearTimeout(copyModeTimer);
      copyModeTimer = setTimeout(() => {
        if (inCopyMode) { ws.send('q'); inCopyMode = false; }
      }, 3000);
    }, { passive: false });

    term.onData(() => {
      if (inCopyMode) { inCopyMode = false; clearTimeout(copyModeTimer); }
    });

    // Mobile: translate touch swipes into tmux copy-mode scroll
    let touchY = null;
    const SCROLL_PX = 30;

    termEl.addEventListener('touchstart', (e) => {
      if (e.touches.length === 1) touchY = e.touches[0].clientY;
    }, { passive: true });

    termEl.addEventListener('touchmove', (e) => {
      if (touchY === null || !ws || ws.readyState !== WebSocket.OPEN) return;
      const dy = touchY - e.touches[0].clientY;
      if (Math.abs(dy) >= SCROLL_PX) {
        const ticks = Math.floor(Math.abs(dy) / SCROLL_PX);
        if (!inCopyMode) {
          ws.send('\\x02[');
          inCopyMode = true;
        }
        const arrow = dy > 0 ? '\\x1b[A' : '\\x1b[B';
        for (let i = 0; i < ticks; i++) ws.send(arrow);
        touchY = e.touches[0].clientY;
        clearTimeout(copyModeTimer);
        copyModeTimer = setTimeout(() => {
          if (inCopyMode) { ws.send('q'); inCopyMode = false; }
        }, 3000);
      }
    }, { passive: true });

    termEl.addEventListener('touchend', () => { touchY = null; }, { passive: true });

    connect();
  </script>
</body>
</html>`;
}


// --- Live-reload ---

const liveReloadClients = new Set();
let reloadTimeout = null;

function broadcastReload(changedFile) {
  if (reloadTimeout) clearTimeout(reloadTimeout);
  reloadTimeout = setTimeout(() => {
    console.log(`[livereload] ${changedFile || 'file changed'} — notifying ${liveReloadClients.size} client(s)`);
    for (const ws of liveReloadClients) {
      try { ws.send('reload'); } catch { liveReloadClients.delete(ws); }
    }
  }, 150);
}

let reloadTimer;
try {
  watch(SITE_DIR, { recursive: true }, (eventType, filename) => {
    clearTimeout(reloadTimer);
    reloadTimer = setTimeout(() => broadcastReload(filename), 400);
  });
  console.log(`[livereload] watching ${SITE_DIR}`);
} catch (err) {
  console.log(`[livereload] fs.watch failed: ${err.message}`);
}

const LIVERELOAD_SCRIPT = `<script>(function(){var p=location.protocol==='https:'?'wss:':'ws:';function connect(){var ws=new WebSocket(p+'//'+location.host+'/livereload');ws.onmessage=function(){location.reload()};ws.onclose=function(){setTimeout(connect,3000)}}connect()})();</script>`;

// --- Tool spawn handler ---

async function handleToolSpawn(req, res) {
  let body = '';
  req.on('data', chunk => body += chunk);
  req.on('end', () => {
    try {
      const { name, command, port } = JSON.parse(body);
      if (!name || !command || !port) {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'name, command, and port required' }));
        return;
      }
      if (toolRegistry.has(name)) {
        res.writeHead(409, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: `${name} already running` }));
        return;
      }
      const child = spawn('sh', ['-c', command], {
        cwd: WOLT_DIR,
        env: { ...process.env, PORT: String(port) },
        stdio: 'pipe',
        detached: true,
      });
      child.unref();
      registerTool(name, port, child.pid, command);
      child.on('exit', () => unregisterTool(name));
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ name, port, pid: child.pid }));
    } catch (err) {
      res.writeHead(500, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: err.message }));
    }
  });
}

function proxyToolHTTP(toolName, req, res, url) {
  const tool = toolRegistry.get(toolName);
  if (!tool) { res.writeHead(404); res.end('tool not found'); return; }

  const targetPath = url.pathname + (url.search || '');
  const proxyReq = httpRequest({
    hostname: '127.0.0.1',
    port: tool.port,
    path: targetPath,
    method: req.method,
    headers: { ...req.headers, host: `127.0.0.1:${tool.port}` },
  }, (proxyRes) => {
    const headers = { ...proxyRes.headers };
    if (headers.location) {
      headers.location = headers.location.replace(
        /^https?:\/\/127\.0\.0\.1:\d+/,
        ''
      );
    }
    res.writeHead(proxyRes.statusCode, headers);
    proxyRes.pipe(res);
  });

  req.pipe(proxyReq);
  proxyReq.on('error', () => {
    if (!res.headersSent) res.writeHead(502);
    res.end('tool unavailable');
  });
}

function proxyToolWebSocket(req, socket, head, pathname) {
  const toolName = pathname.split('/')[2];
  const tool = toolRegistry.get(toolName);
  if (!tool) { socket.destroy(); return; }

  const targetPath = pathname;
  const proxySocket = createConnection({ port: tool.port, host: '127.0.0.1' }, () => {
    const reqLine = `${req.method} ${targetPath} HTTP/1.1\r\n`;
    const rewrittenHeaders = {
      ...req.headers,
      host: `127.0.0.1:${tool.port}`,
      origin: `http://127.0.0.1:${tool.port}`,
    };
    const headers = Object.entries(rewrittenHeaders).map(([k, v]) => `${k}: ${v}`).join('\r\n');
    proxySocket.write(reqLine + headers + '\r\n\r\n');
    if (head.length) proxySocket.write(head);
    socket.pipe(proxySocket).pipe(socket);
  });
  proxySocket.on('error', () => socket.destroy());
  socket.on('error', () => proxySocket.destroy());
}

// --- Static file serving ---

// Serve platform UI (split view) — baked into image, not wolt content
async function servePlatformUI(res) {
  try {
    const content = await readFile(join(PUBLIC_DIR, 'split.html'), 'utf8');
    res.writeHead(200, { 'Content-Type': 'text/html', 'Cache-Control': 'no-cache, no-store, must-revalidate' });
    res.end(content);
    return true;
  } catch { return false; }
}

// Serve wolt content from wolt/site/
async function serveStatic(url, res, req) {
  const fullPath = join(SITE_DIR, url);
  try {
    const content = await readFile(fullPath);
    const ext = extname(fullPath);
    const isIframe = req?.headers?.['sec-fetch-dest'] === 'iframe';
    if (ext === '.html' && WebSocketServer && !isIframe) {
      const html = content.toString();
      const injected = html.includes('</body>')
        ? html.replace('</body>', LIVERELOAD_SCRIPT + '</body>')
        : html + LIVERELOAD_SCRIPT;
      res.writeHead(200, { 'Content-Type': 'text/html', 'Cache-Control': 'no-cache, no-store, must-revalidate' });
      res.end(injected);
    } else {
      res.writeHead(200, { 'Content-Type': MIME[ext] || 'application/octet-stream', 'Cache-Control': 'no-cache, no-store, must-revalidate' });
      res.end(content);
    }
    return true;
  } catch { return false; }
}

// --- HTTP server ---

const server = createServer(async (req, res) => {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');

  if (req.method === 'OPTIONS') { res.writeHead(204); res.end(); return; }

  const url = new URL(req.url, `http://localhost:${PORT}`);

  if (url.pathname === '/version') { res.writeHead(200); res.end('woltspace-v1'); return; }

  // --- Current view (split view control) ---
  if (req.method === 'POST' && url.pathname === '/current') {
    let body = '';
    req.on('data', c => body += c);
    req.on('end', () => {
      const { url: newUrl, title } = JSON.parse(body || '{}');
      if (newUrl) {
        setCurrentUrl(newUrl);
        logView(newUrl, title);
      }
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ url: getCurrentUrl() }));
    });
    return;
  }
  if (req.method === 'GET' && url.pathname === '/status') {
    const status = existsSync(STATUS_FILE) ? JSON.parse(readFileSync(STATUS_FILE, 'utf8')) : {};
    const latestSpark = (() => {
      try {
        const files = readdirSync(SPARKS_DIR).filter(f => f.endsWith('.json'))
          .sort((a,b) => statSync(join(SPARKS_DIR, b)).mtimeMs - statSync(join(SPARKS_DIR, a)).mtimeMs);
        if (!files.length) return null;
        const d = JSON.parse(readFileSync(join(SPARKS_DIR, files[0]), 'utf8'));
        return { id: d.id, title: d.title, timestamp: d.timestamp, report: d.report || null };
      } catch { return null; }
    })();
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      wolt: WOLT_NAME,
      digest: status.digest || { state: 'unknown' },
      currentView: getCurrentUrl(),
      latestSpark,
      serverUptime: Math.floor(process.uptime()),
      updatedAt: status.updatedAt,
    }, null, 2));
    return;
  }
  if (req.method === 'GET' && url.pathname === '/views/history') {
    const entries = readViewsHistory(100);
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(entries));
    return;
  }
  if (req.method === 'GET' && url.pathname === '/current/meta') {
    const data = existsSync(CURRENT_URL_FILE)
      ? JSON.parse(readFileSync(CURRENT_URL_FILE, 'utf8'))
      : { url: '/index.html', updated: 0 };
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(data));
    return;
  }
  if (req.method === 'GET' && url.pathname === '/current') {
    res.writeHead(302, { Location: getCurrentUrl() });
    res.end();
    return;
  }

  // --- Tools (proxy + registry) ---
  if (req.method === 'POST' && url.pathname === '/tools/spawn') return handleToolSpawn(req, res);

  if (req.method === 'GET') {
    if (url.pathname === '/tui') {
      if (!WebSocketServer || !pty) {
        res.writeHead(503, { 'Content-Type': 'text/plain' });
        res.end('TUI not available — ws/node-pty not installed');
        return;
      }
      const sessionName = url.searchParams.get('session') || 'main';
      res.writeHead(200, { 'Content-Type': 'text/html' });
      res.end(tuiHtml(sessionName));
      return;
    }
    // --- Sparks/digests ---
    if (url.pathname === '/history') {
      const sparks = await listSparks();
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(sparks));
      return;
    }
    if (url.pathname.match(/^\/history\/[^/]+\/meta$/)) {
      const id = url.pathname.split('/')[2];
      try {
        const meta = await getSparkWithChain(id);
        const { html, ...metaOnly } = meta;
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify(metaOnly));
      } catch {
        res.writeHead(404, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'not found' }));
      }
      return;
    }
    // --- Sessions (list active tmux sessions) ---
    if (url.pathname === '/sessions') {
      try {
        const raw = execSync('tmux list-sessions -F "#{session_name}|#{session_created}|#{session_windows}|#{session_attached}"', { encoding: 'utf8' }).trim();
        const sessions = raw.split('\n').filter(Boolean).map(line => {
          const [name, created, windows, attached] = line.split('|');
          return { name, created: Number(created), windows: Number(windows), attached: Number(attached) > 0 };
        });
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify(sessions));
      } catch {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end('[]');
      }
      return;
    }

    // --- Tools ---
    if (url.pathname === '/tools') {
      const tools = [];
      for (const [name, info] of toolRegistry) {
        tools.push({ name, port: info.port, pid: info.pid, uptime: Date.now() - info.startedAt });
      }
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(tools));
      return;
    }
    if (url.pathname.startsWith('/tools/')) {
      return proxyToolHTTP(url.pathname.split('/')[2], req, res, url);
    }
    if (url.pathname.startsWith('/history/')) {
      const id = url.pathname.slice('/history/'.length);
      try {
        const data = await getSparkWithChain(id);
        res.writeHead(200, {
          'Content-Type': 'text/html',
          'x-spark-id': data.id,
          'x-spark-parent': data.parentId || '',
          'x-spark-child': data.childId || '',
          'x-spark-version': String(data.version),
          'x-spark-total': String(data.totalVersions),
        });
        res.end(data.html);
      } catch {
        res.writeHead(404);
        res.end('spark not found');
      }
      return;
    }

    // / serves the platform split view; everything else is wolt content
    if (url.pathname === '/') {
      const served = await servePlatformUI(res);
      if (!served) { res.writeHead(500); res.end('Platform UI not found'); }
    } else {
      const served = await serveStatic(url.pathname, res, req);
      if (!served) { res.writeHead(404); res.end('Not found'); }
    }
    return;
  }

  res.writeHead(405);
  res.end('Method not allowed');
});

// --- TUI WebSocket handler ---

if (WebSocketServer && pty) {
  const wss = new WebSocketServer({ noServer: true });

  function ensureTmuxSession(name = 'main') {
    // Sanitize: only allow alphanumeric, dash, underscore
    const safe = name.replace(/[^a-zA-Z0-9_-]/g, '');
    try {
      execSync(`tmux has-session -t ${safe} 2>/dev/null`);
    } catch {
      execSync(`tmux new-session -d -s ${safe} -c ${WOLT_DIR}`);
    }
    return safe;
  }

  wss.on('connection', (ws, req) => {
    const wsUrl = new URL(req.url, `http://localhost:${PORT}`);
    const sessionName = ensureTmuxSession(wsUrl.searchParams.get('session') || 'main');
    console.log(`[tui] client connected → session: ${sessionName}`);

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
      console.log('[tui] pty exited');
      try { ws.close(); } catch {}
    });

    ws.on('message', (msg) => {
      if (typeof msg === 'string' || (msg instanceof Buffer && msg[0] === 0x7b)) {
        try {
          const parsed = JSON.parse(msg.toString());
          if (parsed.type === 'resize' && parsed.cols && parsed.rows) {
            shell.resize(parsed.cols, parsed.rows);
            return;
          }
        } catch {}
      }
      shell.write(msg.toString());
    });

    ws.on('close', () => {
      console.log('[tui] client disconnected');
      shell.kill();
    });
  });

  const lrWss = new WebSocketServer({ noServer: true });
  lrWss.on('connection', (ws) => {
    liveReloadClients.add(ws);
    ws.on('close', () => liveReloadClients.delete(ws));
    ws.on('error', () => liveReloadClients.delete(ws));
  });

  server.on('upgrade', (req, socket, head) => {
    const url = new URL(req.url, `http://localhost:${PORT}`);
    if (url.pathname === '/tui') {
      wss.handleUpgrade(req, socket, head, (ws) => {
        wss.emit('connection', ws, req);
      });
    } else if (url.pathname === '/livereload') {
      lrWss.handleUpgrade(req, socket, head, (ws) => {
        lrWss.emit('connection', ws, req);
      });
    } else if (url.pathname.startsWith('/tools/')) {
      proxyToolWebSocket(req, socket, head, url.pathname);
    } else {
      socket.destroy();
    }
  });
}

server.listen(PORT, () => {
  console.log(`
  woltspace server · http://localhost:${PORT}
  wolt: ${WOLT_NAME}

  endpoints:
    /              — site
    /tui           — browser terminal (tmux)
    /history       — sparks viewer
    /status        — status dashboard
    /current       — panel control
    /tools         — running tools
    /tools/spawn   — start a tool (POST)
  `);

  // --- Digest cron ---
  const DIGEST_SCRIPT = join(__dirname, 'cron', 'digest.mjs');
  const DIGEST_FLAG   = join(STATE_DIR, 'digest-last-run.txt');

  function writeStatus(patch) {
    try {
      const cur = existsSync(STATUS_FILE) ? JSON.parse(readFileSync(STATUS_FILE, 'utf8')) : {};
      writeFileSync(STATUS_FILE, JSON.stringify({ ...cur, ...patch, updatedAt: Date.now() }));
    } catch {}
  }

  function reconcileDigestState() {
    try {
      if (!existsSync(STATUS_FILE)) return;
      const s = JSON.parse(readFileSync(STATUS_FILE, 'utf8'));
      if (s.digest?.state !== 'running') return;
      const pid = s.digest.pid;
      let pidAlive = false;
      if (pid) {
        try { process.kill(pid, 0); pidAlive = true; } catch {}
      }
      if (!pidAlive) {
        const startedAt = s.digest.startedAt || 0;
        const newSparks = readdirSync(SPARKS_DIR).filter(f =>
          f.startsWith('digest-') && statSync(join(SPARKS_DIR, f)).mtimeMs > startedAt
        );
        const resolved = newSparks.length > 0 ? 'done' : 'crashed';
        writeStatus({ digest: { ...s.digest, state: resolved, pid: null, reconciledAt: Date.now() } });
        console.log(`[cron] reconciled digest state: ${s.digest.state} → ${resolved}`);
      }
    } catch {}
  }
  reconcileDigestState();

  function loadDotEnv() {
    const envFile = join(WOLT_DIR, '.env');
    if (!existsSync(envFile)) return {};
    return Object.fromEntries(
      readFileSync(envFile, 'utf8').trim().split('\n')
        .filter(l => l && !l.startsWith('#'))
        .map(l => { const eq = l.indexOf('='); return eq > 0 ? [l.slice(0, eq), l.slice(eq + 1)] : null; })
        .filter(Boolean)
    );
  }

  function spawnDigest(reason) {
    if (!existsSync(DIGEST_SCRIPT)) {
      console.log(`[cron] digest script not found at ${DIGEST_SCRIPT}`);
      return;
    }
    console.log(`[cron] running digest (${reason})`);
    const dotEnv = loadDotEnv();
    const cleanEnv = {
      ...Object.fromEntries(Object.entries(process.env).filter(([k]) => !k.startsWith('CLAUDE'))),
      CLAUDE_CODE_OAUTH_TOKEN: process.env.CLAUDE_CODE_OAUTH_TOKEN || dotEnv.CLAUDE_CODE_OAUTH_TOKEN,
      SPOTIFY_ID: process.env.SPOTIFY_ID || dotEnv.SPOTIFY_ID,
      SPOTIFY_SECRET: process.env.SPOTIFY_SECRET || dotEnv.SPOTIFY_SECRET,
      SPOTIFY_ACCESS_TOKEN: process.env.SPOTIFY_ACCESS_TOKEN || dotEnv.SPOTIFY_ACCESS_TOKEN,
      SPOTIFY_REFRESH_TOKEN: process.env.SPOTIFY_REFRESH_TOKEN || dotEnv.SPOTIFY_REFRESH_TOKEN,
      SPOTIFY_USER: process.env.SPOTIFY_USER || dotEnv.SPOTIFY_USER,
      WOLT_NAME: WOLT_NAME,
      WOLT_DIR: WOLT_DIR,
      NODE_PATH: '/app/node_modules',
    };
    const child = spawn('node', [DIGEST_SCRIPT], { env: cleanEnv, stdio: 'inherit', detached: false });
    writeStatus({ digest: { state: 'running', startedAt: Date.now(), reason, pid: child.pid } });
    child.on('exit', code => {
      console.log(`[cron] digest exited (${code})`);
      writeStatus({ digest: { state: code === 0 ? 'done' : 'failed', exitCode: code, finishedAt: Date.now(), reason } });
    });
  }

  function montrealDateStr() {
    return new Intl.DateTimeFormat('en-CA', { timeZone: 'America/Montreal',
      year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date());
  }
  function montrealHour() {
    return parseInt(new Intl.DateTimeFormat('en-CA', {
      timeZone: 'America/Montreal', hour: 'numeric', hour12: false }).format(new Date()));
  }

  // Digest cron — opt-in via ENABLE_DIGEST_CRON=true in .env
  const digestEnabled = (process.env.ENABLE_DIGEST_CRON || loadDotEnv().ENABLE_DIGEST_CRON || '').toLowerCase() === 'true';

  if (digestEnabled) {
    setInterval(() => {
      const h = montrealHour();
      const today = montrealDateStr();
      const lastRun = existsSync(DIGEST_FLAG)
        ? readFileSync(DIGEST_FLAG, 'utf8').trim() : '';
      if (h >= 6 && lastRun !== today) {
        writeFileSync(DIGEST_FLAG, today);
        spawnDigest('6am daily');
      }
    }, 60 * 1000);

    const DIGEST_3PM_FLAG = join(STATE_DIR, 'digest-3pm-run.txt');
    setInterval(() => {
      const h = montrealHour();
      const today = montrealDateStr();
      const lastRun = existsSync(DIGEST_3PM_FLAG)
        ? readFileSync(DIGEST_3PM_FLAG, 'utf8').trim() : '';
      if (h >= 15 && lastRun !== today) {
        writeFileSync(DIGEST_3PM_FLAG, today);
        spawnDigest('3pm afternoon');
      }
    }, 60 * 1000);

    const testFlag = join(STATE_DIR, 'digest-test-fired.txt');
    if (!existsSync(testFlag)) {
      setTimeout(() => {
        writeFileSync(testFlag, new Date().toISOString());
        spawnDigest('5-minute test');
      }, 5 * 60 * 1000);
      console.log('[cron] one-time digest test scheduled in 5 minutes');
    }
    console.log('[cron] digest cron enabled');
  }
});
