import assert from 'node:assert/strict';
import { readFile, mkdir } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { resolve } from 'node:path';
import { parseEnv } from 'node:util';

const require = createRequire(resolve('artifacts/strategy-control-room/package.json'));
const { chromium } = require('@playwright/test');
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const errors = [];
page.on('pageerror', error => errors.push(error.message));
const origin = 'http://127.0.0.1:8088';
// Keep the automated route sweep below the existing per-identity API limit.
// Responses are not intercepted or retried, so real HTTP failures stay visible.
let nextApiRequestAt = 0;
await page.route(`${origin}/api/**`, async route => {
  const now = Date.now();
  const at = Math.max(now, nextApiRequestAt);
  nextApiRequestAt = at + 750;
  if (at > now) await new Promise(resolve => setTimeout(resolve, at - now));
  await route.continue();
});
const routes = {
  markets: ['iex', 'consolidated', 'history', 'context'],
  portfolio: ['ledger', 'equity'], learning: ['cycles', 'training', 'evaluation', 'performance'],
  research: ['runs', 'experiments', 'library', 'models', 'signals', 'memory'],
  risk: ['readiness', 'limits', 'broker', 'recovery'], activity: ['notifications', 'audit'],
  system: ['health', 'monitoring', 'hardening', 'live', 'access'],
};
async function fits() {
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `Page overflow at ${page.url()}`);
}
try {
  await mkdir('output/paper-ui', { recursive: true });
  await page.goto(origin);
  await page.getByRole('heading', {name:'Overview', exact:true}).waitFor();
  await page.getByText('Research snapshot', {exact:true}).waitFor();
  assert.equal(await page.getByRole('button', {name:'Sign in',exact:true}).count(), 0);
  assert.equal(await page.getByRole('region', {name:'IEX research feed'}).count(), 0);
  await page.screenshot({path:'output/paper-ui/desktop.png',fullPage:true});
  assert.equal((await page.request.get(`${origin}/api/auth/session`)).status(), 200);
  const session = await (await page.request.get(`${origin}/api/auth/session`)).json();
  assert.equal(session.role, 'viewer');
  assert.equal((await page.request.get('http://127.0.0.1:8010/api/dashboard')).status(), 401);
  assert.equal((await page.request.get(`${origin}/api/dashboard`, {headers:{Authorization:'Bearer invalid-key'}})).status(), 401);
  assert.equal((await page.request.post(`${origin}/api/market-data/iex/collect`)).status(), 401);

  for (const width of [1440, 390]) {
    await page.setViewportSize({width, height: width === 390 ? 844 : 1000});
    for (const [group, sections] of Object.entries(routes)) {
      await page.goto(`${origin}/${group}/${sections[0]}`);
      await page.locator('.page-panels').waitFor();
      for (const section of sections) {
        await page.locator(`.page-tabs a[href="/${group}/${section}"]`).click();
        await page.locator('.page-panels').waitFor();
        await page.waitForLoadState('networkidle', {timeout:20000});
        await fits();
        if (group === 'portfolio' && section === 'ledger') {
          const ledgerResponse = await page.request.get(`${origin}/api/stock-paper/status`);
          assert.equal(ledgerResponse.status(), 200);
          const ledger = await ledgerResponse.json();
          const observed = page.getByRole('region', {name:'Observed paper performance'});
          await observed.getByText('Provisional, not qualified performance', {exact:true}).waitFor();
          if (ledger.observed_performance.status === 'provisional') {
          await observed.getByText('Equity change excluding funding', {exact:true}).waitFor();
          await observed.getByText('Reported fee subtotal, already included', {exact:true}).waitFor();
          const missingCommissions = ledger.observed_performance.new_fills_without_commission;
          await observed.getByText(missingCommissions
            ? `Total costs unverified; ${missingCommissions} fills without reported commission`
            : 'Total costs unverified', {exact:true}).waitFor();
          await observed.getByText(/account remains halted and trading is not authorized/).waitFor();
          } else {
            assert.equal(ledger.status, 'halted');
            assert(ledger.observed_performance.reason);
            await observed.getByText(ledger.observed_performance.reason, {exact:true}).waitFor();
            assert.equal(await observed.getByText('Equity change excluding funding', {exact:true}).count(), 0);
            assert.equal(ledger.paper_research_venue.execution_authorized, false);
            assert.equal(ledger.paper_research_venue.live_authorized, false);
          }
          await observed.screenshot({path:`output/paper-ui/observed-${width}.png`});
          const costs = page.getByRole('region', {name:'Modeled cost sensitivity'});
          await costs.getByText('Hypothetical, not realized profit', {exact:true}).waitFor();
          assert.equal(await costs.getByRole('row').count(), 5);
          await costs.screenshot({path:`output/paper-ui/costs-${width}.png`});
        }
        if (section === sections[0]) await page.screenshot({path:`output/paper-ui/${group}-${width}.png`});
      }
    }
  }
  await page.setViewportSize({width:1440,height:1000});
  await page.goto(`${origin}/markets/iex`);
  const feed = page.getByRole('region', {name:'IEX research feed'});
  await feed.getByText('Polling', {exact:false}).waitFor({timeout:30000});
  const learning = page.getByLabel('Online research learning', {exact:true});
  await feed.getByLabel('IEX symbol').selectOption('AAPL');
  await learning.getByText('AAPL Forward Learning', {exact:true}).waitFor();
  await learning.getByText('50/50 baseline: 0.2500', {exact:true}).waitFor();
  await learning.getByText(/^Beats 50\/50: (Yes|No)$/).waitFor();
  const comparison = learning.getByRole('region', {name:'Forward strategy comparison'});
  await comparison.getByRole('heading', {name:'Strategy comparison',exact:true}).waitFor();
  await comparison.getByText(/Direction forecasts, not trade returns/).waitFor();
  assert.equal(await feed.getByRole('button',{name:'Collect',exact:true}).count(),0);
  await page.screenshot({path:'output/paper-ui/markets.png',fullPage:true});
  await page.getByRole('link', {name:'Delayed SIP',exact:true}).click();
  const sip = page.getByRole('region',{name:'Delayed SIP research feed'});
  await sip.getByRole('rowheader',{name:'SPY',exact:true}).waitFor();
  await page.setViewportSize({width:390,height:844});
  await fits();
  await page.screenshot({path:'output/paper-ui/markets-mobile.png',fullPage:true});
  await page.goto(origin);
  await page.getByText('Research snapshot',{exact:true}).waitFor();
  await page.screenshot({path:'output/paper-ui/mobile.png',fullPage:true});
  await page.getByRole('button',{name:'Open navigation',exact:true}).click();
  await page.getByRole('navigation',{name:'Main navigation'}).getByRole('link',{name:'Learning',exact:true}).click();
  await page.waitForFunction(() => document.querySelector('[aria-label="Open navigation"]')?.getAttribute('aria-expanded') === 'false');
  assert.equal(await page.getByRole('button',{name:'Open navigation'}).getAttribute('aria-expanded'),'false');
  await page.reload();
  await page.locator('.page-panels').waitFor();
  await fits();

  await page.goto(`${origin}/system/access`);
  const config = parseEnv(await readFile('.env','utf8'));
  for (const src of await page.locator('script[src]').evaluateAll(nodes => nodes.map(node => node.src))) {
    const code = await (await page.request.get(src)).text();
    for (const name of ['AUTH_VIEWER_KEY','AUTH_RESEARCHER_KEY','AUTH_ADMIN_KEY']) {
      assert(config[name] && !code.includes(config[name]), 'A role key must not appear in browser assets');
    }
  }
  await page.getByLabel('Role access key').fill(config.AUTH_RESEARCHER_KEY);
  await page.getByRole('button',{name:'Apply key',exact:true}).click();
  await page.getByRole('button',{name:'Return to read-only access',exact:true}).waitFor();
  await page.reload();
  await page.getByLabel('Role access key').waitFor();
  assert.equal(await page.getByRole('button',{name:'Return to read-only access',exact:true}).count(),0);
  assert.equal(errors.length,0,errors.join('\n'));
  console.log(JSON.stringify({automatic_viewer:'passed',write_protection:'passed',sections:27,viewports:[1440,390],market_data:'passed',temporary_permissions:'passed',runtime_errors:0}));
} catch (error) {
  console.error(`UI smoke failed at ${new URL(page.url()).pathname}`);
  throw error;
} finally { await browser.close(); }
