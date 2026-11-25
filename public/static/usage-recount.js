// Recount schedule for the usage page. The page shows numbers only while they are fresh:
// a count is requested when the page opens, every 2 minutes while the tab is visible,
// and numbers older than STALE_MS are taken off the screen, never shown.
(function (root) {
  'use strict';
  const RECOUNT_MS = 120000; // recount every 2 minutes while visible
  const STALE_MS = 150000;   // older numbers are hidden ("Counting…") until a new count arrives
  const RETRY_MS = 30000;    // wait after a failed count before the next try

  function createRecounter({load, show, counting, failed, now = () => Date.now(),
                            setTimer = (fn, ms) => setTimeout(fn, ms), clearTimer = id => clearTimeout(id)}) {
    let countedAt = null; // client time at which the shown count was requested
    let failedAt = null;
    let pending = null;
    let revision = 0;
    let expiry = null;    // fires at the shown count's own deadline, whatever the tick phase

    const fresh = () => countedAt !== null && now() - countedAt < STALE_MS;

    function expire() {
      if (countedAt !== null && !fresh()) {
        countedAt = null;
        counting();
      }
      if (countedAt === null && expiry !== null) {
        clearTimer(expiry);
        expiry = null;
      }
    }

    function armExpiry() {
      if (expiry !== null) clearTimer(expiry);
      expiry = setTimer(() => { // a timer that fires early re-arms itself
        expiry = null;
        expire();
        if (countedAt !== null) armExpiry();
      }, Math.max(0, countedAt + STALE_MS - now()));
    }

    function recount() {
      if (pending) return pending;
      const started = now();
      const requested = revision;
      if (countedAt === null) counting();
      pending = new Promise(resolve => resolve(load())) // load starts now; a throw becomes a failure
        .then(data => {
          if (requested !== revision) return;
          if (now() - started >= STALE_MS) { // already old when it arrived
            countedAt = null;
            counting();
            return;
          }
          show(data);
          countedAt = started;
          failedAt = null;
          armExpiry();
        })
        .catch(error => {
          if (requested !== revision) return;
          countedAt = null;
          failedAt = now();
          expire();
          failed(error);
        })
        .finally(() => { if (requested === revision) pending = null; });
      return pending;
    }

    // Call on a timer, on visibilitychange and on pageshow. Hidden tabs do no counting.
    function tick(hidden) {
      expire();
      if (hidden || pending) return null;
      const due = countedAt !== null ? countedAt + RECOUNT_MS
        : failedAt !== null ? failedAt + RETRY_MS : -Infinity;
      return now() >= due ? recount() : null;
    }

    function invalidate() {
      revision++;
      pending = null;
      failedAt = null;
      countedAt = null;
      expire();
      counting();
    }

    return {tick, recount, fresh, invalidate};
  }

  const api = {createRecounter, RECOUNT_MS, STALE_MS, RETRY_MS};
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.UsageRecount = api;
})(this);
