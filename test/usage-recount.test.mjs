// The usage page's no-stale rule (public/static/usage-recount.js), driven with a fake clock:
// numbers show only while fresh, recount every 2 minutes while visible, never while hidden.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

// Loaded the way the page loads it: a classic script that sets window.UsageRecount.
const page = {};
vm.runInNewContext(readFileSync(new URL('../public/static/usage-recount.js', import.meta.url), 'utf8'), page);
const {createRecounter, RECOUNT_MS, STALE_MS, RETRY_MS} = page.UsageRecount;

function harness() {
  const h = {t: 0, loads: [], screen: 'blank', shown: null, timers: new Map(), nextTimer: 1};
  h.r = createRecounter({
    now: () => h.t,
    setTimer: (fn, ms) => { h.timers.set(h.nextTimer, {at: h.t + ms, fn}); return h.nextTimer++; },
    clearTimer: id => h.timers.delete(id),
    load: () => new Promise((resolve, reject) => h.loads.push({resolve, reject})),
    show: data => { h.screen = 'numbers'; h.shown = data; },
    counting: () => { h.screen = 'counting'; h.shown = null; },
    failed: () => { h.screen = 'failed'; h.shown = null; },
  });
  h.finish = async (value, ok = true) => {
    const load = h.loads.at(-1);
    ok ? load.resolve(value) : load.reject(new Error('count failed'));
    await new Promise(resolve => setImmediate(resolve));
  };
  // Move the clock to `to`, firing due timers in order (as the browser would, with no tick).
  h.advance = to => {
    for (;;) {
      const due = [...h.timers].filter(([, timer]) => timer.at <= to).sort((a, b) => a[1].at - b[1].at)[0];
      if (!due) break;
      h.timers.delete(due[0]);
      h.t = Math.max(h.t, due[1].at);
      due[1].fn();
    }
    h.t = to;
  };
  return h;
}

const cases = {
  async 'opening shows Counting… until the first count arrives'() {
    const h = harness();
    h.r.tick(false);
    assert.equal(h.screen, 'counting');
    assert.equal(h.loads.length, 1);
    h.t = 3900;
    h.r.tick(false);
    assert.equal(h.loads.length, 1, 'no second count while one runs');
    await h.finish('fresh-1');
    assert.deepEqual([h.screen, h.shown], ['numbers', 'fresh-1']);
  },
  async 'recounts every 2 minutes while visible, keeping fresh numbers meanwhile'() {
    const h = harness();
    h.r.tick(false); await h.finish('one');
    h.t = RECOUNT_MS - 1; h.r.tick(false);
    assert.equal(h.loads.length, 1);
    h.t = RECOUNT_MS; h.r.tick(false);
    assert.equal(h.loads.length, 2);
    assert.equal(h.screen, 'numbers', 'numbers 2 min old are still fresh');
    await h.finish('two');
    assert.equal(h.shown, 'two');
  },
  async 'a hidden tab does no counting and old numbers come off the screen'() {
    const h = harness();
    h.r.tick(false); await h.finish('one');
    h.t = 10 * 60000; h.r.tick(true);
    assert.equal(h.loads.length, 1, 'hidden: no count');
    assert.equal(h.screen, 'counting', 'stale numbers are hidden, never shown');
    h.r.tick(false);
    assert.equal(h.loads.length, 2);
    assert.equal(h.screen, 'counting');
    await h.finish('fresh');
    assert.equal(h.shown, 'fresh');
  },
  async 'numbers expire at STALE_MS even if a recount hangs'() {
    const h = harness();
    h.r.tick(false); await h.finish('one');
    h.t = RECOUNT_MS; h.r.tick(false);
    h.t = STALE_MS - 1; h.r.tick(false);
    assert.equal(h.screen, 'numbers');
    h.t = STALE_MS; h.r.tick(false);
    assert.equal(h.screen, 'counting');
  },
  async 'numbers come off at the exact cutoff between ticks, with a recount pending'() {
    const h = harness();
    h.t = 1234; h.r.tick(false); await h.finish('one'); // off the 5 s tick phase
    h.advance(1234 + RECOUNT_MS); h.r.tick(false);      // recount starts, stays pending
    assert.equal(h.loads.length, 2);
    h.advance(1234 + STALE_MS - 1);
    assert.equal(h.screen, 'numbers');
    h.advance(1234 + STALE_MS);                         // no tick: only the expiry timer runs
    assert.equal(h.screen, 'counting');
    await h.finish('two');                              // started 30 s ago: fresh, so shown
    assert.deepEqual([h.screen, h.shown], ['numbers', 'two']);
  },
  async 'each new count moves the cutoff to its own deadline'() {
    const h = harness();
    h.t = 700; h.r.tick(false); await h.finish('one');
    h.advance(700 + RECOUNT_MS); h.r.tick(false); await h.finish('two');
    h.advance(700 + STALE_MS);
    assert.equal(h.screen, 'numbers', 'the first count\'s timer must not hide the second');
    h.advance(700 + RECOUNT_MS + STALE_MS);
    assert.equal(h.screen, 'counting');
  },
  async 'a count that arrives too late is not shown'() {
    const h = harness();
    h.r.tick(false);
    h.t = STALE_MS; await h.finish('late');
    assert.equal(h.screen, 'counting');
    assert.equal(h.shown, null);
  },
  async 'a failed count hides the numbers and retries after 30 s'() {
    const h = harness();
    h.r.tick(false); await h.finish('one');
    h.t = RECOUNT_MS; h.r.tick(false); await h.finish(null, false);
    assert.equal(h.screen, 'failed');
    h.t += RETRY_MS - 1; h.r.tick(false);
    assert.equal(h.loads.length, 2);
    h.t += 1; h.r.tick(false);
    assert.equal(h.loads.length, 3);
    assert.equal(h.screen, 'counting');
    await h.finish('back');
    assert.equal(h.shown, 'back');
  },
  async 'a payload the page cannot render counts as a failure'() {
    const h = harness();
    const r = createRecounter({
      now: () => 0, load: () => ({}), counting() {},
      show: () => { throw new Error('Unsupported usage data'); },
      failed: () => { h.screen = 'failed'; },
    });
    await r.tick(false);
    assert.equal(h.screen, 'failed');
    assert.equal(r.fresh(), false);
  },
};

for (const [name, run] of Object.entries(cases)) test(name, run);

test('superseded filter requests cannot display results or clear the active request', async () => {
  const h = harness();
  const old = h.r.tick(false);
  h.r.invalidate();
  const current = h.r.tick(false);
  h.loads[0].resolve('obsolete');
  await old;
  assert.equal(h.shown, null);
  assert.equal(h.r.recount(), current);
  await h.finish('current');
  assert.equal(h.shown, 'current');
});
