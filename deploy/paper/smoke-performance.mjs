import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';

const require = createRequire(resolve('artifacts/strategy-control-room/package.json'));
const { chromium } = require('@playwright/test');
const browser = await chromium.launch({headless: true});
const page = await browser.newPage();
const errors = [];
page.on('pageerror', error => errors.push(error.message));
const origin = 'http://127.0.0.1:8088';
try {
  const response = await page.request.get(`${origin}/api/stock-paper/status`);
  assert.equal(response.status(), 200);
  const ledger = await response.json();
  const performance = ledger.observed_performance;
  assert.equal(performance.status, 'provisional');
  assert.equal(performance.qualifying, false);
  assert.equal(ledger.costs_known, false);
  assert.equal(ledger.account.accounting_verified, false);
  const money = new Intl.NumberFormat('en-US', {style: 'currency', currency: performance.currency});
  await mkdir('output/paper-ui', {recursive:true});
  for (const width of [1440, 390]) {
    await page.setViewportSize({width, height:900});
    await page.goto(`${origin}/portfolio/ledger`);
    const panel = page.getByRole('region', {name:'Observed paper performance'});
    await panel.getByText('Provisional, not qualified performance', {exact:true}).waitFor();
    assert.equal(await panel.locator('dd').first().innerText(), money.format(Number(performance.net_change)));
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await panel.screenshot({path:`output/paper-ui/performance-${width}.png`});
  }
  await page.route('**/api/stock-paper/status', route => route.fulfill({
    contentType:'application/json', body:JSON.stringify({...ledger, observed_performance:{
      status:'unavailable', qualifying:false, scope:'observed_since_initialization',
      net_change:null, reason:'Equity snapshots do not match the reconciliation boundaries',
    }}),
  }));
  await page.reload();
  const panel = page.getByRole('region', {name:'Observed paper performance'});
  await panel.getByText('Equity snapshots do not match the reconciliation boundaries', {exact:true}).waitFor();
  assert.equal(await panel.locator('dd').count(), 0);
  assert.equal(errors.length, 0, errors.join('\n'));
  console.log(JSON.stringify({real_account_projection:'passed', viewports:[1440,390],
    invalid_evidence_fixture:'passed', qualification_unchanged:true, runtime_errors:0}));
} finally {
  await browser.close();
}
