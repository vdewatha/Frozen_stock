import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';

const require = createRequire(resolve('artifacts/strategy-control-room/package.json'));
const { chromium } = require('@playwright/test');
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage();
const errors = [];
page.on('pageerror', error => errors.push(error.message));
const origin = 'http://127.0.0.1:8088';
try {
  await mkdir('output/paper-ui', { recursive: true });
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    await page.goto(`${origin}/markets/iex`);
    const panel = page.getByRole('region', { name: 'Forward shadow returns' });
    await panel.getByRole('heading', { name: 'Shadow price returns' }).waitFor();
    await panel.getByText(/not broker profit/).waitFor();
    await page.waitForLoadState('networkidle');
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await panel.screenshot({ path: `output/paper-ui/shadow-actual-${width}.png` });
    const returns = page.getByRole('region', { name: 'Cost-aware return learner' });
    await returns.getByText(/not broker profit/).waitFor();
    await returns.getByRole('heading', { name: 'Ten-minute evaluation sample' }).waitFor();
    await returns.screenshot({ path: `output/paper-ui/return-actual-${width}.png` });
  }
  const response = await page.request.get(`${origin}/api/market-data/iex/status`);
  assert.equal(response.status(), 200);
  const fixture = await response.json();
  // Browser-only fixture; never POSTed or persisted as research evidence.
  for (const symbol of fixture.learning.symbols) {
    symbol.return_challenger = { paired_observations: 10, unavailable_observations: 1,
      invalid_observations: 0, mae_bps: 7.5, zero_baseline_mae_bps: 8,
      latest_prediction: { training_examples: 70, minimum_training: 50, action: 'cash', predicted_gross_bps: 2 },
      nonoverlapping: { selected_forecasts: 4, paired_observations: 1, pending: 1,
        expired: 1, unavailable_observations: 1, invalid_declarations: 0, invalid_observations: 0,
        strategies: [{ name: 'challenger', long_observations: 0, mean_net_bps: 0 }] },
      strategies: [{ name: 'challenger', long_observations: 2, mean_net_bps: -1.2 },
        { name: 'always_long', long_observations: 10, mean_net_bps: -3 }] };
    symbol.shadow_returns = { paired_observations: 3, unavailable_observations: 2, invalid_observations: 1,
      strategies: [{ name: 'momentum_5m', long_observations: 2, mean_gross_bps: 7,
        mean_net_bps_by_cost: { '0': 7, '1': 4.99, '5': -3.05, '10': -13.1 } }] };
  }
  await page.route('**/api/market-data/iex/status', route => route.fulfill({ json: fixture }));
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    await page.goto(`${origin}/markets/iex`);
    const panel = page.getByRole('region', { name: 'Forward shadow returns' });
    await panel.getByRole('cell', { name: '-3.05', exact: true }).waitFor();
    await panel.getByLabel('Shadow cost per side').selectOption('10');
    await panel.getByRole('cell', { name: '-13.10', exact: true }).waitFor();
    await panel.getByLabel('Shadow cost per side').selectOption('0');
    assert.equal(await panel.getByRole('cell', { name: '7.00', exact: true }).count(), 2);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await panel.screenshot({ path: `output/paper-ui/shadow-fixture-${width}.png` });
    const returns = page.getByRole('region', { name: 'Cost-aware return learner' });
    await returns.getByRole('cell', { name: '-1.20', exact: true }).waitFor();
    await returns.getByText(/Selected: 4\. Paired: 1\. Pending: 1\. Expired: 1\. Missing: 1/).waitFor();
    await returns.screenshot({ path: `output/paper-ui/return-fixture-${width}.png` });
  }
  assert.deepEqual(errors, []);
  console.log('Shadow UI passed: actual and browser-only fixture, desktop/mobile, cost selection, no overflow or page errors.');
} finally {
  await browser.close();
}
