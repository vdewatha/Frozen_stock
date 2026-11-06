import assert from "node:assert/strict";
import test from "node:test";
import { renderToStaticMarkup } from "react-dom/server";
import { WeeklyReportView, type WeeklyReport } from "./weekly-report-panel";

const report: WeeklyReport = {
  week: "2026-W45", generated_at: "2026-11-07T14:07:00Z",
  this_week: { trades: 4, win_rate: 0.75, total_pnl: 12, avg_pnl: 3 },
  all_time: { trades: 36, win_rate: 0.6, total_pnl: 97.4, avg_pnl: 2.7, paper_bps: 8 },
  goal: { target: "profit_after_costs", label: "Profit after costs; win rate is context", min_trades_for_evidence: 30 },
  strategies: [{ strategy_id: 9, strategy_name: "RSI(2) Pullback", status: "paper_trading_candidate", week_trades: 4, week_pnl: 12,
    trades: 34, win_rate: 0.62, total_pnl: 105.4, paper_bps: 9, backtest_bps: 15, backtest_source: "reports/rsi2-scope-20261102.md",
    verdict: "on track with its backtest" }],
  blocked_buys: { submitted_buys: 1, blocked_buys: 2, top_reasons: [{ reason: "earnings on <date> fall within the <n>-session hold window", count: 2 }] },
  short_pilot: { enabled: true, gross_pl: "-0.01", filled_orders: 2 },
  open_unrealized_pnl: 14.5,
};

test("weekly report shows paper results next to the backtest and the blocked reasons", () => {
  const html = renderToStaticMarkup(<WeeklyReportView report={report} stored />);
  assert.match(html, /Week 2026-W45/);
  assert.match(html, /Stored weekly report/);
  assert.match(html, /RSI\(2\) Pullback/);
  assert.match(html, /\+9\.0 bps/);
  assert.match(html, /\+15\.0 bps/);
  assert.match(html, /on track with its backtest/);
  assert.match(html, /earnings on &lt;date&gt;/);
  assert.match(html, /costs unverified/);
});

test("an empty account shows no invented trades", () => {
  const html = renderToStaticMarkup(<WeeklyReportView report={{ ...report, strategies: [], blocked_buys: { submitted_buys: 0, blocked_buys: 0, top_reasons: [] }, short_pilot: null }} stored={false} />);
  assert.match(html, /No closed trades yet/);
  assert.match(html, /No blocked buy signals recorded this week/);
  assert.match(html, /Live preview/);
  assert.doesNotMatch(html, /Short automation/);
});
