import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';

const navigationSource = readFileSync(
  new URL('../public/static/navigation.js', import.meta.url),
  'utf8',
);
const lodgeSource = readFileSync(
  new URL('../public/static/lodge.js', import.meta.url),
  'utf8',
);
const tuiSource = readFileSync(
  new URL('../templates/tui.html', import.meta.url),
  'utf8',
);
const wolvesSource = readFileSync(
  new URL('../public/static/wolves.js', import.meta.url),
  'utf8',
);
const woltsPageSource = readFileSync(
  new URL('../public/static/wolts-page.js', import.meta.url),
  'utf8',
);
const woltPageSource = readFileSync(
  new URL('../public/static/wolt-page.js', import.meta.url),
  'utf8',
);

function loadNavigation() {
  const calls = [];
  const popup = { opener: 'parent' };
  const window = {
    location: {
      assign: url => calls.push(['assign', url]),
      replace: url => calls.push(['replace', url]),
    },
    open: (...args) => {
      calls.push(['open', ...args]);
      return popup;
    },
  };
  runInNewContext(navigationSource, { window });
  return { navigation: window.WoltspaceNavigation, calls, popup };
}

function functionSource(source, name) {
  const start = source.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `${name} found`);
  const body = source.indexOf('{', start);
  let depth = 0;
  for (let i = body; i < source.length; i += 1) {
    if (source[i] === '{') depth += 1;
    if (source[i] === '}') depth -= 1;
    if (depth === 0) return source.slice(start, i + 1);
  }
  throw new Error(`${name} is incomplete`);
}

function loadSessionState(nowSeconds) {
  const context = {
    Date: class extends Date { static now() { return nowSeconds * 1000; } },
  };
  const source = [
    functionSource(lodgeSource, 'timeAgo'),
    functionSource(lodgeSource, 'sessionIsOnline'),
    functionSource(lodgeSource, 'sessionStateText'),
    functionSource(lodgeSource, 'woltStateText'),
    'globalThis.state = { sessionIsOnline, sessionStateText, woltStateText };',
  ].join('\n');
  runInNewContext(source, context);
  return context.state;
}

test('internal navigation reuses the current client', () => {
  const { navigation, calls } = loadNavigation();
  navigation.internal('/tui?session=codexw-123');
  assert.deepEqual(calls, [['assign', '/tui?session=codexw-123']]);
});

test('replace navigation is available for native-shell redirects', () => {
  const { navigation, calls } = loadNavigation();
  navigation.internal('/settings', { replace: true });
  assert.deepEqual(calls, [['replace', '/settings']]);
});

test('external navigation is isolated in a new browser context', () => {
  const { navigation, calls, popup } = loadNavigation();
  navigation.external('https://docs.example.test');
  assert.deepEqual(calls, [[
    'open',
    'https://docs.example.test',
    '_blank',
    'noopener,noreferrer',
  ]]);
  assert.equal(popup.opener, null);
});

test('app destinations prefer the URL supplied by the server', () => {
  const { navigation } = loadNavigation();
  assert.equal(
    navigation.appDestination({ name: 'demo', navigation_url: 'https://demo.example.test/' }),
    'https://demo.example.test/',
  );
  assert.equal(
    navigation.appDestination({ name: 'demo', url: '/app/demo/' }),
    '/app/demo/',
  );
  assert.equal(navigation.appDestination({ name: 'space app' }), '/app/space%20app/');
});

test('lodge contains no client-built localhost app address or internal popups', () => {
  assert.doesNotMatch(lodgeSource, /\.localhost:7777/);
  assert.doesNotMatch(lodgeSource, /window\.open\(\s*['"`]\/tui/);
  assert.match(lodgeSource, /WoltspaceNavigation\.appDestination\(p\)/);
});

test('hostile app metadata is assigned as text and never embedded in handlers', () => {
  const hostile = `')-alert(1)-('`;
  assert.match(lodgeSource, /lodgeElement\(p\.running \? 'a' : 'div', 'app-name-link', p\.name\)/);
  assert.match(lodgeSource, /element\.textContent = text/);
  assert.doesNotMatch(lodgeSource, /\son(?:click|keydown|change|submit)=/i);
  assert.doesNotMatch(lodgeSource, new RegExp(`on\\w+[^\\n]*${hostile.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`));
  assert.doesNotMatch(lodgeSource, /onclick[^\n]*(?:p\.name|keeper|s\.name|sessionFilterWolt)/);
});

test('starter welcome card treats wolt metadata as text and uses the open flow', () => {
  const source = functionSource(lodgeSource, 'renderStarterWelcome');
  assert.match(source, /lodgeElement\('button', 'home-starter-button', `Say hi to \$\{shown\}`\)/);
  assert.match(source, /bubble\.append\(`Hey! I'm \$\{shown\}\.`/);
  assert.match(source, /woltSpriteElement\(wolt\.type, 132\)/);
  assert.match(source, /card\.replaceChildren\(hero\)/);
  assert.doesNotMatch(source, /wolt\.description|home-starter-(?:name|description|heading)/);
  assert.match(source, /button\.addEventListener\('click'/);
  assert.match(source, /startSession\(name\)/);
  assert.doesNotMatch(source, /innerHTML|onclick\s*=/);
});

test('backend harness labels and wolf status remain data', () => {
  assert.match(lodgeSource, /button\.appendChild\(lodgeElement\('span', '', name\)\)/);
  assert.doesNotMatch(lodgeSource, /button\.innerHTML\s*=.*\$\{name\}/);
  assert.match(wolvesSource, /\$\{esc\(e\.status\)\}/);
  assert.match(wolvesSource, /st\.textContent = e\.status \|\| ''/);
  assert.doesNotMatch(wolvesSource, /\$\{e\.status \|\| ''\}/);
  assert.doesNotMatch(wolvesSource, /st\.innerHTML = e\.status/);
});

test('hostile history metadata is text, not executable markup or handler source', () => {
  assert.match(tuiSource, /title\.textContent = s\.title \|\| ''/);
  assert.match(tuiSource, /item\.addEventListener\('click', \(\) => loadSpark\(s\.id, s\.title \|\| ''\)\)/);
  assert.doesNotMatch(tuiSource, /onclick="loadSpark/);
  assert.doesNotMatch(tuiSource, /histList\.innerHTML/);
  assert.doesNotMatch(tuiSource, /<span class="hist-title">\$\{s\.title\}/);
});

test('lodge session consumers use the cached light projection', () => {
  assert.match(lodgeSource, /fetch\('\/sessions\?view=lodge'\)/);
  assert.match(lodgeSource, /sessionStorage\.getItem\(LODGE_SESSIONS_CACHE\)/);
  assert.match(lodgeSource, /restoreLodgeSessions\(\);\s*loadHarnesses/s);
  assert.match(woltPageSource, /fetchJSON\('\/sessions\?view=lodge'/);
  assert.doesNotMatch(woltsPageSource, /prompt_preview|s\.prompt/);
  assert.doesNotMatch(woltPageSource, /prompt_preview|s\.prompt/);
});

test('session and wolt state is only online or offline', () => {
  const now = 10_000_000;
  const { sessionIsOnline, sessionStateText, woltStateText } = loadSessionState(now);
  const online = { status: 'running', alive: true, last_activity: 1 };
  const offline = { status: 'failed', alive: false, last_activity: now - 7200 };

  assert.equal(sessionIsOnline(online), true);
  assert.equal(sessionStateText(online), 'online');
  assert.equal(sessionStateText(offline), 'offline · 2h ago');
  assert.equal(woltStateText({ online: [online], last: 1 }), 'online');
  assert.equal(woltStateText({ online: [], last: now - 7200 }), 'offline · 2h ago');
  assert.equal(woltStateText({ online: [], last: 0 }), 'offline');
});

test('a skewed browser clock cannot turn an online session offline', () => {
  const ancient = { status: 'running', alive: true, last_activity: 1 };
  assert.equal(loadSessionState(100).sessionStateText(ancient), 'online');
  assert.equal(loadSessionState(10_000_000_000).sessionStateText(ancient), 'online');
  const stateSource = [
    functionSource(lodgeSource, 'sessionIsOnline'),
    functionSource(lodgeSource, 'sessionStateText'),
    functionSource(lodgeSource, 'woltStateText'),
  ].join('\n');
  assert.doesNotMatch(stateSource, /sessionIsWorking|idle_seconds|\bworking\b|\bawake\b/i);
});

test('wolt listings use the shared state vocabulary', () => {
  assert.match(woltsPageSource, /group\('Online', online\)/);
  assert.match(woltsPageSource, /group\('Offline', offline, false\)/);
  assert.doesNotMatch(woltsPageSource, /\bAwake\b|\bResting\b/);
  assert.doesNotMatch(woltPageSource, /sessionIsWorking|\bworking\b|\bawake\b/i);
});

test('terminal opening checks only its session before attach or resume', () => {
  assert.match(tuiSource, /fetch\('\/sessions\/' \+ encodeURIComponent\(session\)\)/);
  assert.match(tuiSource, /s\.agent_alive === true/);
  assert.doesNotMatch(tuiSource, /fetch\('\/sessions'\)/);
  assert.match(tuiSource, /fetch\('\/sessions\/' \+ encodeURIComponent\(session\) \+ '\/resume'/);
});

async function runEnsureSessionAlive(record, resume = { ok: true, body: { status: 'running' } }) {
  const source = tuiSource.match(/async function ensureSessionAlive\(\) \{[\s\S]*?^  \}/m)?.[0];
  assert.ok(source, 'ensureSessionAlive function found');
  const calls = [];
  const context = {
    session: 'n00b-one',
    connectTUI: () => calls.push(['attach']),
    setStatus: (...args) => calls.push(['status', ...args]),
    encodeURIComponent,
    setTimeout: callback => { callback(); return 1; },
    fetch: async (url, options = {}) => {
      calls.push(['fetch', url, options.method || 'GET']);
      if (url.endsWith('/resume')) {
        if (resume.throw) throw new Error('resume request failed');
        return { ok: resume.ok, json: async () => resume.body };
      }
      return { ok: true, json: async () => record };
    },
  };
  await runInNewContext(`${source}; ensureSessionAlive()`, context);
  return calls;
}

test('terminal attaches to a running session with a live agent', async () => {
  const calls = await runEnsureSessionAlive({ status: 'running', agent_alive: true });
  assert.deepEqual(calls, [
    ['fetch', '/sessions/n00b-one', 'GET'],
    ['attach'],
  ]);
});

test('terminal resumes a tmux-alive session whose agent exited', async () => {
  const calls = await runEnsureSessionAlive({
    status: 'running', agent_alive: false, tmux_alive: true,
  });
  assert.deepEqual(calls, [
    ['fetch', '/sessions/n00b-one', 'GET'],
    ['status', 'connecting', 'resuming'],
    ['fetch', '/sessions/n00b-one/resume', 'POST'],
    ['attach'],
  ]);
});

test('terminal attaches to live tmux when resume fails', async () => {
  const calls = await runEnsureSessionAlive(
    { status: 'running', agent_alive: false, tmux_alive: true },
    { ok: false, body: { error: 'no conversation id' } },
  );
  assert.deepEqual(calls, [
    ['fetch', '/sessions/n00b-one', 'GET'],
    ['status', 'connecting', 'resuming'],
    ['fetch', '/sessions/n00b-one/resume', 'POST'],
    ['attach'],
  ]);
});

test('terminal reports resume failure when tmux is gone', async () => {
  const calls = await runEnsureSessionAlive(
    { status: 'orphaned', agent_alive: false, tmux_alive: false },
    { ok: false, body: { error: 'no conversation id' } },
  );
  assert.deepEqual(calls.at(-1), ['status', 'disconnected', 'resume failed']);
  assert.equal(calls.some(call => call[0] === 'attach'), false);
});

test('terminal attaches to live tmux when resume request throws', async () => {
  const calls = await runEnsureSessionAlive(
    { status: 'running', agent_alive: false, tmux_alive: true },
    { throw: true },
  );
  assert.deepEqual(calls.at(-1), ['attach']);
});
