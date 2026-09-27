// Render-level acceptance check. Run with BASE_URL pointed at a scratch lodge:
// NODE_PATH=<playwright node_modules> BASE_URL=http://127.0.0.1:17799 node test/browser/lodge_wolt_page.cjs
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const base = process.env.BASE_URL || 'http://127.0.0.1:17799';
const wolt = process.env.WOLT_NAME || 'n00b';
(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 844 } });
    await page.route('**/sessions', route => route.fulfill({ contentType: 'application/json', body: JSON.stringify([{
      name: `${wolt}-hostile-a1b2c3`, wolt, status: 'running', alive: true,
      created_at: Date.now() / 1000, title: '<img src=x onerror=alert(1)>', summary: '<script>alert(2)</script>',
    }]) }));
    await page.goto(`${base}/w/${encodeURIComponent(wolt)}`, { waitUntil: 'networkidle' });
    await page.waitForTimeout(1000);
    assert.equal(await page.locator('#bg-nature svg').count(), 1);
    assert.equal(await page.locator('.wolt-session-list img').count(), 0);
    assert.equal(await page.locator('.session-title').first().textContent(), '<img src=x onerror=alert(1)>');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
