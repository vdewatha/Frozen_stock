// Browser contract test only. Every initialization request is intercepted.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { resolve } from 'node:path';
const require = createRequire(resolve('artifacts/strategy-control-room/package.json'));
const { chromium } = require('@playwright/test');
const browser = await chromium.launch({headless:true});
try {
  const page = await browser.newPage();
  const bodies = [];
  const status = {broker:'alpaca_paper',mode:'paper',status:'uninitialized',costs_known:false,
    legacy_nonqualifying:true,account:null,positions:[],orders:[],fills:[],equity_snapshots:[]};
  await page.route('**/api/auth/session', route => route.fulfill({json:{role:'admin',live_orders_allowed:false}}));
  await page.route('**/api/stock-paper/status', route => route.fulfill({json:status}));
  await page.route('**/api/stock-paper/initialize', route => {
    bodies.push(route.request().postDataJSON());
    return route.fulfill({json:status});
  });
  await page.goto('http://127.0.0.1:8088/portfolio/ledger');
  await page.getByLabel('Accounting contract').selectOption('alpaca-activities-v2');
  await page.getByText('Imports dated activity accounting.',{exact:false}).waitFor();
  const submitted = page.waitForResponse(response => response.url().endsWith('/api/stock-paper/initialize'));
  await page.getByTestId('button-initialize-stock-paper-account').click();
  await submitted;
  assert.deepEqual(bodies,[{activity_contract:'alpaca-activities-v2'}]);
  console.log(JSON.stringify({v2_selection_and_request:'passed',broker_writes:0,identity:'browser_fixture_only'}));
} finally { await browser.close(); }
