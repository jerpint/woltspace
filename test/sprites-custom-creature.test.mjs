import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';

// The lodge pages' shared helpers, from the real sprites.js.
const source = readFileSync(new URL('../public/static/sprites.js', import.meta.url), 'utf8');
const context = {};
runInNewContext(`${source}\nthis.h = { woltEmoji, woltBaseType, woltSpriteAvatar, RODENT_TYPES };`, context);
const { woltEmoji, woltBaseType, woltSpriteAvatar, RODENT_TYPES } = context.h;

test('a built-in wolt keeps its emoji, whatever the lodge sends', () => {
  assert.equal(woltEmoji({ type: 'raccoon', emoji: '⭐' }), '🦝');
  assert.equal(woltEmoji({ type: 'beaver' }), '🦫');
  assert.equal(woltEmoji(undefined), '🦫');
});

test('a custom creature shows its own emoji and behaves like its size', () => {
  const astre = { type: 'star', emoji: '⭐', base_type: 'raccoon' };
  assert.equal(woltEmoji(astre), '⭐');
  assert.equal(woltSpriteAvatar(astre.type, 20), null); // no pixel art: the emoji stands in
  assert.ok(RODENT_TYPES.has(woltBaseType(astre)));
  assert.equal(woltBaseType({ type: 'otter' }), 'otter');
});

// The model pages show for a wolt must be the one its session gets:
// its pin, else its creature's model on that engine, else its size's default.
const codex = { id: 'codex', models: { raccoon: 'gpt-5.5', otter: 'gpt-5.6-luna' },
  catalog: [{ id: 'gpt-5.5' }, { id: 'gpt-5.6-luna' }, { id: 'gpt-6-astra' }] };
const { woltDefaultModel } = (() => {
  const c = {};
  runInNewContext(`${source}\nthis.f = { woltDefaultModel };`, c);
  return c.f;
})();
const star = { type: 'star', base_type: 'raccoon', harness: 'codex', creature_models: { codex: 'gpt-6-astra' } };
const quick = { type: 'quick', base_type: 'otter', harness: 'codex', creature_models: {} };

test('an unpinned custom creature shows its own model, else its size default', () => {
  assert.equal(woltDefaultModel(star, codex), 'gpt-6-astra');
  assert.equal(woltDefaultModel(quick, codex), 'gpt-5.6-luna');
  assert.equal(woltDefaultModel({ ...star, creature_models: { codex: 'gone' } }, codex), 'gpt-5.5');
  assert.equal(woltDefaultModel({ type: 'beaver' }, { ...codex, models: { beaver: 'b' } }), 'b');
  const opencode = { id: 'opencode', freeform_model: true, models: { raccoon: 'openai/gpt-4o' }, catalog: [] };
  const own = { ...star, creature_models: { opencode: 'custom-provider/custom-model' } };
  assert.equal(woltDefaultModel(own, opencode), 'custom-provider/custom-model');
});

function pageExpression(file, marker, context, expr) {
  const text = readFileSync(new URL(`../public/static/${file}`, import.meta.url), 'utf8');
  const line = text.split('\n').find(x => x.includes(marker));
  const ctx = { woltDefaultModel, woltBaseType, ...context };
  runInNewContext(`${line}\nresult = ${expr};`, ctx);
  return ctx.result;
}

test('the wolt page shows the model the session will really get', () => {
  const harnesses = { default: 'claude', harnesses: [codex] };
  const shown = (config) => pageExpression('wolt-page.js', 'const effective =', { config, harnesses }, 'effective().model');
  assert.equal(shown(star), 'gpt-6-astra');
  assert.equal(shown(quick), 'gpt-5.6-luna');
  assert.equal(shown({ ...star, model: 'gpt-5.5' }), 'gpt-5.5');
});

test('the lodge engine picker and card use the same precedence', () => {
  const lodge = readFileSync(new URL('../public/static/lodge.js', import.meta.url), 'utf8');
  const start = lodge.indexOf('function woltHarness(');
  const body = lodge.slice(start, lodge.indexOf('\n}\n', start) + 2);
  const ctx = { woltDefaultModel, woltBaseType, harnessDefault: 'codex', harnessInfo: () => codex };
  runInNewContext(`${body}\nthis.woltHarness = woltHarness;`, ctx);
  assert.equal(ctx.woltHarness(star).model, 'gpt-6-astra');
  assert.equal(ctx.woltHarness(quick).model, 'gpt-5.6-luna');
  assert.equal(ctx.woltHarness({ ...star, model: 'gpt-5.5' }).model, 'gpt-5.5');
});

test('the wolves page shows a custom creature with its emoji', () => {
  const emoji = (WOLTS, EMOJI_BY_TYPE) => pageExpression('wolves.js', 'const EMOJI =', { WOLTS, EMOJI_BY_TYPE }, 'EMOJI');
  const shown = emoji([{ name: 'nova', type: 'star', emoji: '⭐' }, { name: 'ux', type: 'raccoon', emoji: '🦝' },
    { name: 'old', type: 'spider' }], { raccoon: '🦝' });
  assert.deepEqual({ ...shown }, { nova: '⭐', ux: '🦝', old: '🐾' });
});
