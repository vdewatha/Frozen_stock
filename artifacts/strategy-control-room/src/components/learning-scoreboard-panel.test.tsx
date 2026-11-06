import assert from "node:assert/strict";
import test from "node:test";
import { renderToStaticMarkup } from "react-dom/server";
import { LearningScoreboardView, type LearningScoreboard, type Totals } from "./learning-scoreboard-panel";

const none: Totals = { trades: 0, wins: 0, win_rate: null, avg_return: null, net_pnl: null };
const some: Totals = { trades: 20, wins: 7, win_rate: 0.35, avg_return: -0.0042, net_pnl: -295 };

const board: LearningScoreboard = {
  generated_at: "2026-11-06T20:30:00Z",
  health: { session: { date: "2026-11-06", in_progress: false }, checks: [
    { key: "explained", label: "Closed trades are explained", status: "ok", detail: "All 3 closed trades of the last 10 days are explained." },
    { key: "news_fed", label: "News symbols get live prices", status: "failing", detail: "No minute bars this session for SOFI." },
    { key: "news_sized", label: "News-found buys are learning-sized", status: "waiting", detail: "No buys in news-found symbols this session yet." },
  ] },
  progress: {
    overall: some,
    weeks: [{ week: "2026-W44", ...some }, { week: "2026-W45", ...none }],
    strategies: [{ strategy_type: "macd_momentum", ...some, recent: some, earlier: none, evidence: "10 more trades before judging" }],
    goal: { target: "profit_after_costs", label: "Profit after costs; win rate is context", min_trades_for_evidence: 30 },
  },
  causes: { diagnosed: 20, by_cause: { signal_wrong: 7, missed_market_move: 3, edge: 7 }, by_family: {}, contributing: { clustered_stops: 11 } },
  repeating_failures: [{ strategy_type: "all", cause: "missed_market_move", count: 3, share_of_losses: 0.4, symbols: ["AAPL", "META"], proposal: "Test a market gate" }],
  news: { observed_at: "2026-11-06T20:10:00Z", status: "complete", error: null, articles_fetched: 300,
    admitted: [{ symbol: "TREX", name: "Trex Co", articles: 4, avg_sentiment: 1, expires_at: "2026-11-09T13:10:00Z", top_headline: "BofA Upgrades Trex to Buy" }],
    holding: ["SOFI"], rejected: [{ symbol: "PENY", reason: "price 2.00 is below 5.00" }] },
  news_vs_core: { news: none, core: some, news_wrong: 0 },
  changes: [{ key: "news_symbols", effective: "2026-11-06", title: "Symbols from the news; one position per symbol", watch: "crowded_symbol disappears", before: some, after: none }],
};

test("the scoreboard answers 'is it working' and 'are we improving'", () => {
  const html = renderToStaticMarkup(<LearningScoreboardView board={board} />);
  assert.match(html, /Is it working\?/);
  assert.match(html, /1 check failing/);
  assert.match(html, /No minute bars this session for SOFI/);
  assert.match(html, /Session 2026-11-06/);
  assert.match(html, /2026-W44/);
  assert.match(html, /-0\.42%/);
  assert.match(html, /The strategy&#x27;s signal was wrong/);
  assert.match(html, /also: clustered stops/);
  assert.match(html, /Test a market gate/);
  assert.match(html, /BofA Upgrades Trex to Buy/);
  assert.match(html, /exits only\): SOFI/);
  assert.match(html, /PENY \(price 2\.00 is below 5\.00\)/);
  assert.match(html, /no trades yet/);
});

test("a fresh deploy shows waiting checks and no invented results", () => {
  const html = renderToStaticMarkup(<LearningScoreboardView board={{
    ...board, health: { session: null, checks: [] }, repeating_failures: [], news: null,
    progress: { ...board.progress, overall: none, strategies: [] }, causes: { diagnosed: 0, by_cause: {}, by_family: {}, contributing: {} },
  }} />);
  assert.match(html, /Nothing failing/);
  assert.match(html, /No session yet/);
  assert.match(html, /No closed trades yet/);
  assert.match(html, /No explained trades yet/);
  assert.match(html, /No repeating failure yet/);
  assert.match(html, /No discovery run yet/);
});
