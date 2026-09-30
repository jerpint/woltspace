// Render-level acceptance check. Run against any scratch lodge; all relevant
// API responses are intercepted so no live colony state is read or changed.
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const base = process.env.BASE_URL || 'http://127.0.0.1:17799';

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 844 } });
    const starter = {
      dir: 'onboardie', name: '<Onboardie>', type: 'beaver', origin: 'starter',
      emoji: '🪵', description: '<img src=x onerror=alert(1)>',
    };
    let wolts = [starter];
    let sessionTotal = 0;
    let spawnBody = null;
    await page.route('**/wolts', route => route.fulfill({
      contentType: 'application/json', body: JSON.stringify(wolts),
    }));
    await page.route('**/onboarding/status', route => route.fulfill({
      contentType: 'application/json', body: JSON.stringify({
        needs_harness_choice: false, harness_selected: true, has_user_wolt: false,
        starter: { state: 'installed' },
      }),
    }));
    await page.route('**/sessions?view=lodge', async route => {
      await new Promise(resolve => setTimeout(resolve, 1200));
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ sessions: [], totals: { onboardie: sessionTotal } }),
      });
    });
    await page.route('**/sessions/new/lodge', async route => {
      spawnBody = route.request().postDataJSON();
      await route.fulfill({
        contentType: 'application/json', body: JSON.stringify({ ok: true }),
      });
    });

    await page.goto(base, { waitUntil: 'domcontentloaded' });
    const card = page.locator('#home-starter-welcome');
    await card.waitFor({ state: 'visible', timeout: 3000 });
    assert.equal(await card.locator('.home-starter-name, .home-starter-description').count(), 0);
    assert.equal(await card.locator('button').textContent(), 'Say hi 🪵');
    assert.equal(await card.textContent(), 'Say hi 🪵');
    assert.equal(await card.locator('img').count(), 0);
    const chrome = await card.evaluate(element => {
      const style = getComputedStyle(element);
      return {
        background: style.backgroundColor,
        border: style.borderTopWidth,
        padding: style.paddingTop,
        shadow: style.boxShadow,
        alignment: style.justifyContent,
      };
    });
    assert.deepEqual(chrome, {
      background: 'rgba(0, 0, 0, 0)', border: '0px', padding: '0px',
      shadow: 'none', alignment: 'center',
    });
    await card.locator('button', { hasText: 'Say hi' }).click();
    assert.deepEqual(spawnBody, { wolt: '<Onboardie>' });
    assert.equal(await card.isVisible(), false);

    sessionTotal = 1;
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(1500);
    assert.equal(await card.isVisible(), false, 'a prior starter session hides the welcome');

    sessionTotal = 0;
    wolts = [starter, { dir: 'mine', name: 'mine', type: 'raccoon', origin: 'user' }];
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(1500);
    assert.equal(await card.isVisible(), false, 'a user-created wolt hides the welcome');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
