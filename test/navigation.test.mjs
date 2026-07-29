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
