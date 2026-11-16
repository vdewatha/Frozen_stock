import assert from "node:assert/strict";
import test from "node:test";
import { renderToStaticMarkup } from "react-dom/server";
import { HypothesisRegisterView, type HypothesisEntry, type HypothesisRegister } from "./hypothesis-register-panel";

const failed: HypothesisEntry = {
  id: "earnings-drift", source: "report", family: "post_earnings_drift", title: "Post-earnings drift",
  hypothesis: "Stocks that jump on an earnings release keep rising.", data: "every US-listed filer since 2016, delisted included",
  status: "failed", result: "+0.76% a hold, -0.44% against SPY.", why: "With delisted stocks the drift does not beat SPY.",
  decided_on: "2026-11-11", next_decision: null, revisit_if: "Intraday reaction data.",
  sources: { reports: ["reports/earnings-drift-delisted-20261111.md"], notes: null, pr: 92 },
  dataset: { snapshots: [{ name: "earnings-drift-delisted-20261111", snapshot_id: "cb6f332389ecdf167f91141c085524f5ac77b4079a5d6d87825ac6c8662f00a2" }], note: "assigned after the study ran" },
  can_place_paper_orders: false, paper_orders: { can_place: false, detail: "research only: no strategy row", rows: [] },
};
const collecting: HypothesisEntry = {
  id: "forward-experiment-2", source: "forward_experiment", family: "weekly_reversal", title: "Forward experiment 2: rule_1cd460d679",
  hypothesis: "BUY when dist_sma_200 > 0 and ret_5 < -0.08", data: "the bot's own paper trades since registration",
  status: "collecting", result: "no review recorded yet; needs 800 trades", why: "Pre-registered and frozen.",
  decided_on: null, next_decision: { date: "2027-02-10", note: "first review no earlier than this date" },
  sample: { required: 800, observed: 12, expected_trades_per_year: 45, years_to_verdict: 17.8 },
  revisit_if: "Nothing on history.", sources: { reports: ["reports/local-sweep-20261110/forward-experiments.md"], notes: null, pr: null },
  can_place_paper_orders: true,
  paper_orders: { can_place: true, detail: "learning-sized exploration entries only",
    rows: [{ strategy_type: "rule_1cd460d679", status: "paper_trading_candidate", can_place: true, detail: "learning-sized exploration entries only" }] },
};
const passed: HypothesisEntry = { ...failed, id: "vix-gate", family: "market_timing_gates", title: "VIX fear gate", status: "passed",
  result: "Adopted for Channel Breakout only.", caveat: "In-sample on a survivor list.", sources: { reports: ["reports/vix-gate-20261106.md"], notes: null, pr: null } };

const register: HypothesisRegister = {
  as_of: "2026-11-16T15:00:00Z", updated: "2026-11-16", statuses: ["passed", "failed", "inconclusive", "collecting"],
  counts: { passed: 1, failed: 1, inconclusive: 0, collecting: 1 }, can_place_paper_orders: 1,
  closed_families: { post_earnings_drift: ["earnings-drift"] }, entries: [failed, collecting, passed],
  unavailable: [{ source: "strategy_discovery", error: "RuntimeError: boom" }],
  note: "passed means a pre-set rule was met on the data named; it is not a claim of profit.",
};

test("the register shows the verdict, the reason, the next decision and whether each hypothesis can trade", () => {
  const html = renderToStaticMarkup(<HypothesisRegisterView register={register} />);
  assert.match(html, /What the research has established/);
  assert.match(html, /3 hypotheses/);
  assert.match(html, /it is not a claim of profit/);
  assert.match(html, /Post-earnings drift/);
  assert.match(html, /With delisted stocks the drift does not beat SPY/);
  assert.match(html, /research only: no strategy row/);
  assert.match(html, /PR #92/);
  assert.match(html, /Data snapshot: earnings-drift-delisted-20261111 \(cb6f332389ec\)/);
  assert.match(html, /needs 800 trades, has 12/);
  assert.match(html, /about 17\.8 years at the expected rate/);
  assert.match(html, /learning-sized exploration entries only/);
  assert.match(html, /Caveat: In-sample on a survivor list/);
  assert.match(html, /Closed families[^<]*post earnings drift/);
  assert.match(html, /strategy discovery unavailable: RuntimeError: boom/);
});

test("filtering by verdict shows only those hypotheses", () => {
  const html = renderToStaticMarkup(<HypothesisRegisterView register={register} filter="collecting" onFilter={() => undefined} />);
  assert.match(html, /Forward experiment 2/);
  assert.doesNotMatch(html, /Post-earnings drift/);
  assert.match(html, /Collecting evidence · 1 shown/);
  assert.match(html, /aria-pressed="true"[^>]*>Collecting evidence/);
  const empty = renderToStaticMarkup(<HypothesisRegisterView register={register} filter="inconclusive" />);
  assert.match(empty, /No hypothesis has this verdict/);
});
