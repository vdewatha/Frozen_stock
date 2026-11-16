import assert from "node:assert/strict";
import test from "node:test";
import { renderToStaticMarkup } from "react-dom/server";
import { LearningTraceView, type LearningChain, type LearningTrace } from "./learning-trace-panel";

const retained: LearningChain = {
  experiment_id: 12, ran_at: "2026-11-08T17:00:00+00:00", strategy_type: "channel_breakout", template: "market_gate",
  diagnosis: { lesson_id: "abc123abc123", cause: "missed_market_move", lesson: "The market fell.", losses: 4, scope: "channel_breakout", source: "trade_attribution", attribution_ids: [1, 2, 3, 4] },
  experiment: { status: "adopted", adopted: "sma20", description: "no new longs while SPY is below its 20-day average", setups: 4, cost_seconds: 95 },
  change: { change_id: "c0ffee000001", adopted_at: "2026-11-08T17:00:00+00:00", description: "no new longs while SPY is below its 20-day average" },
  forward: { state: "confirmed", verdict: "working", judged_at: "2026-11-18T17:00:00+00:00", trades_after: 6, trades_needed: 6, sample_sufficient: true,
    expectancy_before_bps: -90, expectancy_after_bps: 200, before_after_bps: 290, realised_gain_bps: 290, realised_source: "before_after",
    comparison: "before against after: not a control, the market moved too" },
  outcome: "retained", complete: true, gaps: [],
};
const unchanged: LearningChain = { ...retained, experiment_id: 13, strategy_type: "macd_momentum",
  diagnosis: { ...retained.diagnosis, lesson_id: null, attribution_ids: [] },
  experiment: { status: "rejected", adopted: null, description: null, setups: 2, cost_seconds: null },
  change: null, forward: null, outcome: "no_change", complete: false, gaps: ["no lesson id: the experiment was recorded before lessons carried one"] };

const trace: LearningTrace = {
  summary: { chains: 2, with_lesson_id: 1, complete: 1, changes_adopted: 1, outcomes: { no_change: 1, watching: 0, retained: 1, reverted: 0, closed: 0 },
    retained_on_sufficient_sample: 1, research_seconds: 95, experiments_with_cost: 1 },
  evidence: "1 change(s) retained on a sufficient forward sample, mean realised gain +290.0 bps a trade; 0 reverted.",
  chains: [retained, unchanged], unlinked_changes: ["orphan000001"], note: "A chain is diagnosed problem, experiment, change, forward evaluation, outcome.",
};

test("the trace follows a lesson to its experiment, change, forward verdict and outcome", () => {
  const html = renderToStaticMarkup(<LearningTraceView trace={trace} />);
  assert.match(html, /Does the learning improve outcomes\?/);
  assert.match(html, /1 change\(s\) retained on a sufficient forward sample/);
  assert.match(html, /lesson abc123abc123 · 4 trades named/);
  assert.match(html, /channel breakout · market gate/);
  assert.match(html, /4 setups · 1\.6 min/);
  assert.match(html, /6 of 6 trades/);
  assert.match(html, /before -90\.0 bps · after \+200\.0 bps/);
  assert.match(html, /not a control, the market moved too/);
  assert.match(html, /Retained/);
  assert.match(html, /None adopted/);
  assert.match(html, /Gap: no lesson id/);
  assert.match(html, /not recorded/);
  assert.match(html, /Changes with no experiment behind them: orphan000001/);
});

test("an empty trace says nothing has run", () => {
  const empty: LearningTrace = { ...trace, chains: [], unlinked_changes: [], evidence: "No experiment has run yet, so there is nothing to judge.",
    summary: { ...trace.summary, chains: 0, complete: 0, changes_adopted: 0, research_seconds: null, experiments_with_cost: 0 } };
  const html = renderToStaticMarkup(<LearningTraceView trace={empty} />);
  assert.match(html, /No experiment has run yet\./);
  assert.match(html, /nothing to judge/);
});
