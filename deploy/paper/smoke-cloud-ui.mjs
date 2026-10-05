import assert from 'node:assert/strict';
import { mkdir } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { resolve } from 'node:path';

const require = createRequire(resolve('artifacts/strategy-control-room/package.json'));
const { chromium } = require('@playwright/test');
const origin = process.env.CLOUD_PAPER_ORIGIN || 'http://127.0.0.1:18088';
const routes = {
  markets: ['iex', 'consolidated', 'history', 'context'],
  portfolio: ['ledger', 'equity'], learning: ['cycles', 'training', 'evaluation', 'performance'],
  research: ['runs', 'experiments', 'library', 'models', 'signals', 'memory'],
  risk: ['readiness', 'limits', 'broker', 'recovery'], activity: ['notifications', 'audit'],
  system: ['health', 'monitoring', 'hardening', 'live', 'access'],
};
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage();
const errors = [];
const unavailableReads = new Set();
page.on('pageerror', error => errors.push(error.message));
page.on('response', response => {
  if (response.url().startsWith(`${origin}/api/`) && response.request().method() === 'GET'
      && response.status() >= 400) {
    unavailableReads.add(`${response.status()} ${new URL(response.url()).pathname}`);
  }
});
let nextRequest = 0;
await page.route(`${origin}/api/**`, async route => {
  const delay = Math.max(0, nextRequest - Date.now());
  nextRequest = Math.max(Date.now(), nextRequest) + 900;
  if (delay) await new Promise(resolve => setTimeout(resolve, delay));
  await route.continue();
});
try {
  await mkdir('output/cloud-ui', { recursive: true });
  const health = await (await page.request.get(`${origin}/api/health`)).json();
  assert.equal(health.live_trading, false);
  assert.equal(health.paper_only, true);
  const rejected = await page.request.post(`${origin}/api/market-data/iex/collect`);
  assert([401, 403].includes(rejected.status()), 'Anonymous writes must be rejected');
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    await page.goto(origin);
    await page.getByRole('heading', { name: 'Overview', exact: true }).waitFor();
    assert.equal(await page.getByRole('button', { name: 'Sign in', exact: true }).count(), 0);
    for (const [group, sections] of Object.entries(routes)) {
      for (const section of sections) {
        const response = await page.goto(`${origin}/${group}/${section}`);
        assert.equal(response.status(), 200);
        await page.locator('h1').waitFor();
        await page.waitForTimeout(1500);
        assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), page.url());
      }
      await page.screenshot({ path: `output/cloud-ui/${group}-${width}.png`, fullPage: true });
    }
  }
  assert.deepEqual(errors, []);
  console.log('Cloud dashboard: desktop/mobile routes, read-only access and paper-only health: PASS');
  console.log('Unavailable API reads (not proof of readiness):', [...unavailableReads]);
} finally {
  await browser.close();
}
