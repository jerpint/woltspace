import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';
import test from 'node:test';

const script = await readFile(new URL('../public/static/connectors.js', import.meta.url), 'utf8');

class Element {
  children = [];
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  get text() { return [this.textContent, ...this.children.map(child => child.text)].join(' '); }
}

test('WhatsApp card opens the setup session for the selected wolt', async () => {
  const root = new Element();
  const location = {href: ''};
  const calls = [];
  const responses = {
    '/tunnel': {mode: 'off'},
    '/health': {connectors: [{name: 'whatsapp', state: 'disabled'}]},
    '/wolts': [{dir: 'my-wolt', name: 'My Wolt', type: 'custom', base_type: 'otter'}],
    '/sessions/new/lodge': {name: 'my-wolt-setup'},
  };
  await vm.runInNewContext(script, {
    document: {getElementById: () => root, createElement: () => new Element()},
    fetch: async (path, options) => {
      calls.push({path, options});
      return {ok: true, json: async () => responses[path]};
    },
    location,
  });
  const card = root.children.find(child => child.text.includes('WhatsApp'));
  const button = card.children.find(child => child.type === 'button');
  assert.equal(button.textContent, 'Set up with a wolt');
  await button.onclick();
  assert.equal(calls.at(-1).path, '/sessions/new/lodge');
  assert.deepEqual(JSON.parse(calls.at(-1).options.body), {wolt: 'my-wolt', setup: 'whatsapp'});
  assert.equal(location.href, '/tui?session=my-wolt-setup');
});
