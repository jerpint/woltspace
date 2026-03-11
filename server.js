import { createServer } from 'node:http';
import { readFile, writeFile, readdir, mkdir } from 'node:fs/promises';
import { join, extname, resolve as resolvePath } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { existsSync, statSync, readFileSync, readdirSync, writeFileSync, appendFileSync, mkdirSync, watch, unlinkSync } from 'node:fs';
import { execSync, spawn } from 'node:child_process';
import { createConnection } from 'node:net';
import { randomBytes } from 'node:crypto';
import { request as httpRequest } from 'node:http';
import { request as httpsRequest } from 'node:https';

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

function shellQuote(s) {
  return "'" + s.replace(/'/g, "'\\''") + "'";
}

function parseEnvFile(filePath) {
  try {
    const out = {};
    for (const line of readFileSync(filePath, 'utf8').split('\n')) {
      const t = line.trim();
      if (!t || t.startsWith('#')) continue;
      const idx = t.indexOf('=');
      if (idx === -1) continue;
      out[t.slice(0, idx).trim()] = t.slice(idx + 1).trim();
    }
    return out;
  } catch { return {}; }
}
const WOLT_DIR = process.env.WOLT_DIR || __dirname;
const WOLTS_DIR = process.env.WOLTS_DIR || join(WOLT_DIR, '..');
const SITE_DIR = join(WOLT_DIR, 'wolt', 'site');
const APPS_DIR = join(WOLT_DIR, 'wolt', 'apps');
const SPARKS_DIR = join(WOLT_DIR, 'wolt', 'sparks');
const PUBLIC_DIR = join(__dirname, 'public');  // platform UI assets (baked into image)
const STATE_DIR = join(WOLT_DIR, '.state');
const WOLTS_STATE_DIR = join(WOLTS_DIR, '.state');  // shared across wolts (sessions, routing)
const WOLT_NAME = process.env.WOLT_NAME || 'wolt';
const PORT = 3000;

// --- State dir (session data, tool registry, cron flags) ---

const TOOL_REGISTRY_FILE = join(STATE_DIR, 'tool-registry.json');
// Per-session current URL files: current-url-{session}.json (see currentUrlFile())
const VIEWS_HISTORY_FILE = join(STATE_DIR, 'views-history.jsonl');
const STATUS_FILE        = join(STATE_DIR, 'status.json');
const BOT_LOG_FILE       = join(STATE_DIR, 'bot-debug', 'bot.jsonl');
const SHARES_DIR          = join(STATE_DIR, 'shares');

function botLog(event, data) {
  try {
    mkdirSync(join(STATE_DIR, 'bot-debug'), { recursive: true });
    const entry = { ts: new Date().toISOString().replace(/\.\d+Z$/, 'Z'), event, ...data };
    appendFileSync(BOT_LOG_FILE, JSON.stringify(entry) + '\n');
  } catch { /* non-critical */ }
}

async function ensureStateDir() {
  await mkdir(STATE_DIR, { recursive: true });
}

// --- Current view (right pane of split, per-session) ---

function sanitizeSession(name) {
  return (name || '').replace(/[^a-zA-Z0-9_-]/g, '').slice(0, 64) || 'main';
}

function currentUrlFile(session) {
  return join(STATE_DIR, `current-url-${sanitizeSession(session)}.json`);
}

function getCurrentUrl(session = 'main') {
  const f = currentUrlFile(session);
  if (!existsSync(f)) return null;
  try { return JSON.parse(readFileSync(f, 'utf8')).url || null; } catch { return null; }
}

function setCurrentUrl(url, session = 'main', port = 3000) {
  mkdirSync(STATE_DIR, { recursive: true });
  const safe = sanitizeSession(session);
  writeFileSync(currentUrlFile(safe), JSON.stringify({ url, port, updated: Date.now() }));
  console.log(`[current:${safe}] → ${url}`);
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

// Extended MIME types for app assets (fonts, images, etc.)
const APP_MIME = {
  '.woff': 'font/woff', '.woff2': 'font/woff2', '.ttf': 'font/ttf', '.otf': 'font/otf',
  '.ico': 'image/x-icon', '.webp': 'image/webp', '.avif': 'image/avif',
  '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.gif': 'image/gif',
  '.mp4': 'video/mp4', '.webm': 'video/webm', '.mp3': 'audio/mpeg',
  '.wasm': 'application/wasm', '.map': 'application/json',
};

// tuiHtml removed — all TUI pages now use split.html (the split view is the unit)

// --- .env loader (used by notify + digest cron) ---

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

function getEnv(key) {
  return process.env[key] || loadDotEnv()[key] || '';
}

// --- Notify: send a message to the originating chat ---

const SESSION_ROUTING_DIR = join(WOLTS_STATE_DIR, 'session-routing');

function readSessionRouting(session) {
  const f = join(SESSION_ROUTING_DIR, `${sanitizeSession(session)}.json`);
  if (!existsSync(f)) return null;
  try { return JSON.parse(readFileSync(f, 'utf8')); } catch { return null; }
}

// Sentinel footer added to notify messages in Telegram/Slack.
// Used by the adapter to detect replies-to-den (route directly to session).
// Stripped before writing to history so the bot model never sees it and can't reproduce it.
const DEN_REPLY_FOOTER = '\n↩️ reply to this message to talk to this session directly';

function appendChatHistory(adapter, chatId, content) {
  // Write notify messages into the bot's chat history so it sees them on the next turn.
  // Stored as role: "user" with a system context tag so the bot model knows
  // this is a den report (not something it said, not from the human).
  // The DEN_REPLY_FOOTER is stripped — bot never sees it.
  const subdir = adapter === 'slack' ? join(STATE_DIR, 'chat', 'slack') : join(STATE_DIR, 'chat');
  const chatFile = join(subdir, `${chatId}.jsonl`);
  const cleanContent = content.replace(DEN_REPLY_FOOTER, '');
  const entry = {
    role: 'user',
    content: `<system>This message was sent by a Claude Code session directly to the user. It is context only — do not respond to it.</system>\n${cleanContent}`,
    ts: new Date().toISOString(),
  };
  try {
    mkdirSync(subdir, { recursive: true });
    appendFileSync(chatFile, JSON.stringify(entry) + '\n');
  } catch (e) {
    console.error('[notify] failed to append chat history:', e.message);
  }
}

async function sendNotification(session, message) {
  const routing = readSessionRouting(session);

  // Always prefer Telegram — resolve chat_id from routing (if telegram) or fallback.
  const telegramToken = getEnv('TELEGRAM_BOT_TOKEN');
  const telegramChatId = routing?.adapter === 'telegram'
    ? routing.chat_id
    : (() => {
        const allowed = getEnv('TELEGRAM_ALLOWED_USERS').split(',').map(s => s.trim()).filter(Boolean);
        return allowed[0] || null;
      })();

  if (telegramToken && telegramChatId) {
    await telegramSend(telegramToken, telegramChatId, message + DEN_REPLY_FOOTER);
    appendChatHistory('telegram', telegramChatId, message);
    return { adapter: 'telegram', chat_id: telegramChatId };
  }

  // Fallback: Slack (only if Telegram isn't configured)
  if (routing?.adapter === 'slack') {
    const token = getEnv('SLACK_BOT_TOKEN');
    if (!token) throw new Error('SLACK_BOT_TOKEN not set');
    const channel = routing?.channel || getEnv('SLACK_NOTIFY_CHANNEL');
    if (!channel) throw new Error('no slack channel in routing and SLACK_NOTIFY_CHANNEL not set');
    await slackSend(token, channel, routing?.thread_ts || null, message);
    appendChatHistory('slack', channel, message);
    return { adapter: 'slack', channel };
  }

  throw new Error('no notification target — set TELEGRAM_BOT_TOKEN + TELEGRAM_ALLOWED_USERS');
}

function telegramSend(token, chatId, text) {
  return new Promise((resolve, reject) => {
    const body = JSON.stringify({ chat_id: chatId, text });
    const req = httpsRequest({
      hostname: 'api.telegram.org',
      path: `/bot${token}/sendMessage`,
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) },
    }, (res) => {
      let data = '';
      res.on('data', c => data += c);
      res.on('end', () => {
        const parsed = (() => { try { return JSON.parse(data); } catch { return {}; } })();
        if (parsed.ok) resolve(parsed); else reject(new Error(parsed.description || 'telegram error'));
      });
    });
    req.on('error', reject);
    req.end(body);
  });
}

function slackSend(token, channel, threadTs, text) {
  return new Promise((resolve, reject) => {
    const payload = { channel, text };
    if (threadTs) payload.thread_ts = threadTs;
    const body = JSON.stringify(payload);
    const req = httpsRequest({
      hostname: 'slack.com',
      path: '/api/chat.postMessage',
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${token}`,
        'Content-Length': Buffer.byteLength(body),
      },
    }, (res) => {
      let data = '';
      res.on('data', c => data += c);
      res.on('end', () => {
        const parsed = (() => { try { return JSON.parse(data); } catch { return {}; } })();
        if (parsed.ok) resolve(parsed); else reject(new Error(parsed.error || 'slack error'));
      });
    });
    req.on('error', reject);
    req.end(body);
  });
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

// --- App WebSocket proxy (for dev servers with HMR/live-reload) ---

function proxyAppWebSocket(req, socket, head, pathname) {
  const match = pathname.match(/^\/app\/([a-zA-Z][a-zA-Z0-9_-]*)(\/.*)?$/);
  if (!match) { socket.destroy(); return; }
  const appName = match[1];
  const appJsonPath = join(APPS_DIR, appName, 'app.json');
  if (!existsSync(appJsonPath)) { socket.destroy(); return; }
  let config;
  try { config = JSON.parse(readFileSync(appJsonPath, 'utf8')); } catch { socket.destroy(); return; }
  if (!config.port) { socket.destroy(); return; }

  // Strip /app/:name prefix — app sees the bare path
  const targetPath = match[2] || '/';
  const proxySocket = createConnection({ port: config.port, host: '127.0.0.1' }, () => {
    const reqLine = `${req.method} ${targetPath} HTTP/1.1\r\n`;
    const rewrittenHeaders = {
      ...req.headers,
      host: `127.0.0.1:${config.port}`,
      origin: `http://127.0.0.1:${config.port}`,
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

// Serve platform UI files — baked into image, not wolt content
async function servePlatformFile(res, filename) {
  try {
    const content = await readFile(join(PUBLIC_DIR, filename), 'utf8');
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

  // --- Current view (split view control, per-session) ---
  if (req.method === 'POST' && url.pathname === '/current') {
    const session = sanitizeSession(url.searchParams.get('session'));
    let body = '';
    req.on('data', c => body += c);
    req.on('end', () => {
      const { url: newUrl, title, port } = JSON.parse(body || '{}');
      if (newUrl) {
        setCurrentUrl(newUrl, session, port || 3000);
        logView(newUrl, title);
      }
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ url: getCurrentUrl(session) }));
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
      currentView: getCurrentUrl('main'),
      latestSpark,
      serverUptime: Math.floor(process.uptime()),
      updatedAt: status.updatedAt,
    }, null, 2));
    return;
  }
  if (req.method === 'GET' && url.pathname === '/onboard-status') {
    const env = parseEnvFile(join(WOLTS_DIR, '.env'));
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      wolts_dir: WOLTS_DIR,
      wolt_name: WOLT_NAME,
      has_oauth:      existsSync('/home/node/.claude/.credentials.json') || !!(env.CLAUDE_CODE_OAUTH_TOKEN || process.env.CLAUDE_CODE_OAUTH_TOKEN),
      has_human_name: !!(env.HUMAN_NAME && env.HUMAN_NAME.trim() && env.HUMAN_NAME !== 'your-name'),
      has_llm_key:    !!(env.ANTHROPIC_API_KEY || env.OPENROUTER_API_KEY),
      has_telegram:   env.ENABLE_TELEGRAM_BOT === 'true' && !!env.TELEGRAM_BOT_TOKEN,
    }));
    return;
  }
  if (req.method === 'GET' && url.pathname === '/views/history') {
    const entries = readViewsHistory(100);
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(entries));
    return;
  }
  if (req.method === 'GET' && url.pathname === '/current/meta') {
    const session = sanitizeSession(url.searchParams.get('session'));
    const f = currentUrlFile(session);
    const data = existsSync(f)
      ? JSON.parse(readFileSync(f, 'utf8'))
      : { url: null, updated: 0 };
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(data));
    return;
  }
  if (req.method === 'GET' && url.pathname === '/current') {
    const session = sanitizeSession(url.searchParams.get('session'));
    const curUrl = getCurrentUrl(session);
    if (curUrl) {
      res.writeHead(302, { Location: curUrl });
    } else {
      res.writeHead(204);
    }
    res.end();
    return;
  }

  // --- Notify: push message to originating chat ---
  if (req.method === 'POST' && url.pathname === '/notify') {
    let body = '';
    req.on('data', c => body += c);
    req.on('end', async () => {
      try {
        const { session, message } = JSON.parse(body || '{}');
        if (!message) {
          res.writeHead(400, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'message required' }));
          return;
        }
        const result = await sendNotification(session || '', message);
        console.log(`[notify] → ${result.adapter} | ${message.slice(0, 80)}`);
        botLog('notify_sent', { session: session || '', ...result, message });
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: true, ...result }));
      } catch (err) {
        console.error('[notify] error:', err.message);
        res.writeHead(500, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: err.message }));
      }
    });
    return;
  }

  // --- Memory read (wolt/memory/ only, path-whitelist enforced) ---
  if (req.method === 'POST' && url.pathname === '/memory/read') {
    let body = '';
    req.on('data', c => body += c);
    req.on('end', async () => {
      try {
        const { path: memPath } = JSON.parse(body || '{}');
        if (!memPath || typeof memPath !== 'string') {
          res.writeHead(400, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'path required' }));
          return;
        }
        // Security: normalize and ensure path stays within wolt/memory/
        const MEMORY_DIR = join(WOLT_DIR, 'wolt', 'memory');
        const abs = resolvePath(MEMORY_DIR, memPath);
        if (!abs.startsWith(MEMORY_DIR + '/') && abs !== MEMORY_DIR) {
          res.writeHead(403, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'path outside memory directory' }));
          return;
        }
        const content = await readFile(abs, 'utf8');
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ path: memPath, content }));
      } catch (err) {
        const status = err.code === 'ENOENT' ? 404 : 500;
        res.writeHead(status, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: err.code === 'ENOENT' ? 'not found' : err.message }));
      }
    });
    return;
  }

  // --- Session messaging: inject directly into tmux pty ---
  if (req.method === 'POST' && url.pathname.match(/^\/sessions\/[^/]+\/message$/)) {
    const sessionId = sanitizeSession(url.pathname.split('/')[2]);
    let body = '';
    req.on('data', c => body += c);
    req.on('end', () => {
      try {
        const { text } = JSON.parse(body || '{}');
        if (!text) {
          res.writeHead(400, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'text required' }));
          return;
        }
        // Send directly to the tmux session's pty — keys buffer and Claude reads them next
        execSync(`tmux send-keys -t ${shellQuote(sessionId)} -l ${shellQuote(text)}`);
        execSync(`tmux send-keys -t ${shellQuote(sessionId)} Enter`);
        console.log(`[message] → ${sessionId}: ${text.slice(0, 80)}`);
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: true, session: sessionId }));
      } catch (err) {
        res.writeHead(500, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: err.message }));
      }
    });
    return;
  }

  // --- Session spawning ---
  if (req.method === 'POST' && url.pathname === '/sessions/new') {
    let body = '';
    req.on('data', c => body += c);
    req.on('end', () => {
      try {
        const { prompt } = JSON.parse(body || '{}');
        if (!prompt) {
          res.writeHead(400, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'prompt required' }));
          return;
        }
        const sessionName = `${WOLT_NAME}-${Date.now() % 100000}`;
        const runScript = join(__dirname, 'container', 'bin', 'run-session.sh');
        const cmd = `${runScript} ${shellQuote(sessionName)} ${shellQuote(WOLT_DIR)} ${shellQuote(prompt)}`;
        execSync(`tmux new-session -d -s ${sessionName} -c ${shellQuote(WOLT_DIR)} ${shellQuote(cmd)}`);
        console.log(`[sessions] spawned ${sessionName}`);
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ name: sessionName }));
      } catch (err) {
        res.writeHead(500, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: err.message }));
      }
    });
    return;
  }

  // --- Tools (proxy + registry) ---
  if (req.method === 'POST' && url.pathname === '/tools/spawn') return handleToolSpawn(req, res);

  // --- Apps: /app/:name/* → serve from wolt/apps/:name/ ---
  // Requires app.json in the app dir. Serves dist/ (static) or proxies to port (server).
  // See container/skills/apps/SKILL.md for the full protocol.
  const appMatch = url.pathname.match(/^\/app\/([a-zA-Z][a-zA-Z0-9_-]*)(\/.*)?$/);
  if (appMatch) {
    const appName = appMatch[1];
    const appDir = join(APPS_DIR, appName);
    const appJsonPath = join(appDir, 'app.json');

    // Gate: app.json must exist
    if (!existsSync(appJsonPath)) {
      res.writeHead(404, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: `app "${appName}" not found — missing app.json` }));
      return;
    }

    let appConfig;
    try { appConfig = JSON.parse(readFileSync(appJsonPath, 'utf8')); }
    catch { res.writeHead(500); res.end('invalid app.json'); return; }

    const subPath = (appMatch[2] || '/').replace(/\/$/, '') || '/';
    const distDir = join(appDir, 'dist');

    // Strategy 1: static — serve from dist/ if it exists
    if (existsSync(distDir)) {
      const candidates = [join(distDir, subPath), join(distDir, subPath, 'index.html')];
      for (const candidate of candidates) {
        const resolved = resolvePath(candidate);
        // Security: ensure resolved path stays inside distDir
        if (!resolved.startsWith(resolvePath(distDir))) continue;
        if (existsSync(resolved) && statSync(resolved).isFile()) {
          const ext = extname(resolved);
          const mime = MIME[ext] || APP_MIME[ext] || 'application/octet-stream';
          const content = await readFile(resolved);
          res.writeHead(200, { 'Content-Type': mime, 'Cache-Control': 'no-cache' });
          res.end(content);
          return;
        }
      }
      res.writeHead(404); res.end('Not found in app');
      return;
    }

    // Strategy 2: proxy — forward to localhost:port (strip /app/:name prefix)
    const port = appConfig.port;
    if (!port || port < 1024 || port > 65535) {
      res.writeHead(500); res.end(`app "${appName}" has no dist/ and no valid port in app.json`);
      return;
    }
    const targetPath = (subPath === '/' ? '/' : subPath) + (url.search || '');
    const proxyHeaders = { ...req.headers, host: `localhost:${port}` };
    delete proxyHeaders['x-frame-options'];
    const proxyReq = httpRequest({ hostname: 'localhost', port, path: targetPath, method: req.method, headers: proxyHeaders }, (proxyRes) => {
      const respHeaders = { ...proxyRes.headers };
      delete respHeaders['x-frame-options'];
      delete respHeaders['content-security-policy'];
      res.writeHead(proxyRes.statusCode, respHeaders);
      proxyRes.pipe(res);
    });
    proxyReq.on('error', () => { res.writeHead(502); res.end(`App "${appName}" not running on port ${port}`); });
    req.pipe(proxyReq);
    return;
  }


  // --- Share link management ---
  // POST /shares { session, label? } → create token (port resolved from session state)
  if (req.method === 'POST' && url.pathname === '/shares') {
    let body = '';
    req.on('data', c => body += c);
    req.on('end', () => {
      try {
        const { session: shareSession, label } = JSON.parse(body || '{}');
        const targetSession = sanitizeSession(shareSession || 'main');
        // Resolve port from session's current viewport state
        const sessionFile = currentUrlFile(targetSession);
        const sessionData = existsSync(sessionFile)
          ? JSON.parse(readFileSync(sessionFile, 'utf8'))
          : {};
        const port = sessionData.port || 3000;
        const token = targetSession;
        mkdirSync(SHARES_DIR, { recursive: true });
        writeFileSync(
          join(SHARES_DIR, `${token}.json`),
          JSON.stringify({ session: targetSession, port, label: label || null, created: Date.now(), wolt: WOLT_NAME })
        );
        console.log(`[shares] created ${token} → port ${port}`);
        res.writeHead(201, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ token, url: `/public/${token}`, session: targetSession, port }));
      } catch (err) {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: err.message }));
      }
    });
    return;
  }

  // GET /shares → list all tokens with liveness check
  if (req.method === 'GET' && url.pathname === '/shares') {
    try {
      mkdirSync(SHARES_DIR, { recursive: true });
      const files = readdirSync(SHARES_DIR).filter(f => f.endsWith('.json'));
      const shares = await Promise.all(files.map(async f => {
        try {
          const token = f.replace(/\.json$/, '');
          const data = JSON.parse(readFileSync(join(SHARES_DIR, f), 'utf8'));
          const alive = await new Promise(resolve => {
            const sock = createConnection({ port: data.port, host: 'localhost' });
            sock.once('connect', () => { sock.destroy(); resolve(true); });
            sock.once('error', () => resolve(false));
            setTimeout(() => { sock.destroy(); resolve(false); }, 500);
          });
          return { token, ...data, alive };
        } catch { return null; }
      }));
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(shares.filter(Boolean)));
    } catch (err) {
      res.writeHead(500, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: err.message }));
    }
    return;
  }

  // DELETE /shares/:token → revoke
  const deleteShareMatch = url.pathname.match(/^\/shares\/([A-Za-z0-9_-]+)$/);
  if (req.method === 'DELETE' && deleteShareMatch) {
    const token = deleteShareMatch[1];
    const shareFile = join(SHARES_DIR, `${token}.json`);
    if (!existsSync(shareFile)) {
      res.writeHead(404, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: 'share not found' }));
      return;
    }
    try {
      unlinkSync(shareFile);
      console.log(`[shares] revoked token ${token}`);
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: true, token }));
    } catch (err) {
      res.writeHead(500, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: err.message }));
    }
    return;
  }

  // /public/:token/* → public port proxy (no auth — token is the credential)
  const shareProxyMatch = url.pathname.match(/^\/public\/([A-Za-z0-9_-]+)(\/.*)?$/);
  if (shareProxyMatch) {
    const token = shareProxyMatch[1];
    const shareFile = join(SHARES_DIR, `${token}.json`);
    if (!existsSync(shareFile)) {
      res.writeHead(404, { 'Content-Type': 'text/plain' });
      res.end('Share link not found or revoked.');
      return;
    }
    let shareData;
    try { shareData = JSON.parse(readFileSync(shareFile, 'utf8')); }
    catch { res.writeHead(500); res.end('invalid share config'); return; }

    const { port, session: shareSession } = shareData;

    // If no subpath, redirect to the session's current viewport URL
    if (!shareProxyMatch[2]) {
      const sessionFile = currentUrlFile(sanitizeSession(shareSession || 'main'));
      const sessionData = existsSync(sessionFile) ? JSON.parse(readFileSync(sessionFile, 'utf8')) : {};
      const viewportPath = sessionData.url || '/';
      res.writeHead(302, { Location: `/public/${token}${viewportPath}` });
      res.end();
      return;
    }

    const subPath = shareProxyMatch[2] + (url.search || '');
    const proxyHeaders = { ...req.headers, host: `localhost:${port}` };
    const proxyReq = httpRequest({ hostname: 'localhost', port, path: subPath, method: req.method, headers: proxyHeaders }, (proxyRes) => {
      const respHeaders = { ...proxyRes.headers };
      delete respHeaders['x-frame-options'];
      delete respHeaders['content-security-policy'];
      res.writeHead(proxyRes.statusCode, respHeaders);
      proxyRes.pipe(res);
    });
    proxyReq.on('error', () => {
      res.writeHead(502, { 'Content-Type': 'text/plain' });
      res.end(`Service not running on port ${port}.`);
    });
    req.pipe(proxyReq);
    return;
  }



  if (req.method === 'GET') {
    // Homepage — session launcher + history
    if (url.pathname === '/') {
      if (!WebSocketServer || !pty) {
        // Outside Docker — serve the static site instead
        const served = await serveStatic('/index.html', res, req);
        if (!served) { res.writeHead(404); res.end('Not found'); }
        return;
      }
      const served = await servePlatformFile(res, 'home.html');
      if (!served) { res.writeHead(500); res.end('home.html not found'); }
      return;
    }
    if (url.pathname === '/onboard') {
      const served = await servePlatformFile(res, 'onboard.html');
      if (!served) { res.writeHead(500); res.end('onboard.html not found'); }
      return;
    }
    // Split view (terminal + viewport)
    if (url.pathname === '/tui') {
      if (!WebSocketServer || !pty) {
        res.writeHead(503, { 'Content-Type': 'text/plain' });
        res.end('TUI not available — ws/node-pty not installed');
        return;
      }
      const served = await servePlatformFile(res, 'split.html');
      if (!served) { res.writeHead(500); res.end('split.html not found'); }
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
    // --- Apps (list registered apps) ---
    if (url.pathname === '/apps') {
      try {
        const apps = [];
        if (existsSync(APPS_DIR)) {
          for (const name of readdirSync(APPS_DIR)) {
            const appJson = join(APPS_DIR, name, 'app.json');
            if (!existsSync(appJson)) continue;
            try {
              const config = JSON.parse(readFileSync(appJson, 'utf8'));
              const hasDist = existsSync(join(APPS_DIR, name, 'dist'));
              apps.push({ name, url: `/app/${name}/`, mode: hasDist ? 'static' : (config.port ? 'proxy' : 'unconfigured'), ...config });
            } catch {}
          }
        }
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify(apps));
      } catch {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end('[]');
      }
      return;
    }

    // --- Sessions (list with status from .state/sessions/) ---
    if (url.pathname === '/sessions') {
      try {
        // Get live tmux sessions
        const tmuxSessions = new Set();
        try {
          const raw = execSync('tmux list-sessions -F "#{session_name}"', { encoding: 'utf8' }).trim();
          raw.split('\n').filter(Boolean).forEach(n => tmuxSessions.add(n));
        } catch {}

        // Read status files (includes completed sessions)
        const sessionsDir = join(WOLTS_STATE_DIR, 'sessions');
        const sessions = [];
        try {
          const files = readdirSync(sessionsDir).filter(f => f.endsWith('.json'));
          for (const f of files) {
            try {
              const data = JSON.parse(readFileSync(join(sessionsDir, f), 'utf8'));
              // Reconcile status against live tmux sessions
              if (tmuxSessions.has(data.session)) data.status = 'running';
              else if (data.status === 'running') data.status = 'done';
              sessions.push(data);
            } catch {}
          }
        } catch {}

        // Sort: running first, then by start time descending
        sessions.sort((a, b) => {
          if (a.status === 'running' && b.status !== 'running') return -1;
          if (b.status === 'running' && a.status !== 'running') return 1;
          return (b.started || 0) - (a.started || 0);
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

    // Try wolt site first, then platform public dir
    const served = await serveStatic(url.pathname, res, req) || await servePlatformFile(res, url.pathname.slice(1));
    if (!served) { res.writeHead(404); res.end('Not found'); }
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
    } else if (url.pathname.startsWith('/app/')) {
      proxyAppWebSocket(req, socket, head, url.pathname);
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
    /              — session launcher + history
    /tui?session=X — split view (terminal + viewport)
    /sessions      — list sessions (GET) / spawn (POST /sessions/new)
    /history       — sparks viewer
    /status        — status dashboard
    /current?session=X — viewport control
    /shares            — list/create/revoke share tokens (GET/POST/DELETE)
    /public/:token/*   — public port proxy (no auth, token = credential)
    /apps          — list registered apps
    /app/:name/    — serve an app (static or proxy)
    /tools         — running tools
    /tools/spawn   — start a tool (POST)
    /memory/read   — read a memory file (POST {path})
    /notify        — push message to originating chat (POST {session, message})
    /sessions/:id/message — queue a message for delivery on next session idle (POST {text})
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
      NODE_PATH: '/workspace/woltspace/node_modules',
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

 
