// Mobile acceptance check. Run against a scratch lodge with Playwright:
// NODE_PATH=<playwright node_modules> BASE_URL=http://127.0.0.1:17799 node test/browser/lodge_cached_sessions.cjs
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const base = process.env.BASE_URL || 'http://127.0.0.1:17799';
const wolt = process.env.TEST_WOLT || 'n00b';

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
    const cached = {
      sessions: [{
        name: `${wolt}-cached-a1b2c3`, wolt, status: 'running', alive: true,
        last_activity: Date.now() / 1000, title: 'Cached conversation', openable: true,
      }],
      totals: { [wolt]: 1 },
    };
    await page.addInitScript(value => {
      sessionStorage.setItem('woltspace:lodge-sessions:v1', JSON.stringify(value));
    }, cached);

    let freshSessionsArrived = false;
    await page.route('**/sessions?view=lodge', async route => {
      await new Promise(resolve => setTimeout(resolve, 2500));
      freshSessionsArrived = true;
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify(cached) });
    });

    const started = Date.now();
    await page.goto(base, { waitUntil: 'domcontentloaded' });
    const cachedCard = page.locator('#sidebar-wolts .wolt-card', { hasText: wolt }).first();
    await cachedCard.waitFor({ state: 'attached', timeout: 2000 });
    assert.equal(freshSessionsArrived, false, 'sidebar waited for the delayed sessions response');
    assert.ok(Date.now() - started < 2500, 'cached sidebar did not paint before the network response');
    assert.equal(page.viewportSize().width, 390);
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
