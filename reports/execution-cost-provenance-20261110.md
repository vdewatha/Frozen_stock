# Execution-cost provenance: 2026-11-10

**What this answers.** Every verdict in this project turns on a cost number, and the local sweep
(`local-sweep-20261110.md`) flips most families between its two scenarios. This note separates what has been
*measured* from what has been *modelled* or *assumed*, says where each number lives, and lists what is still not
measured. It was written on the research machine without touching the deployed host, so the production tables
that hold the only real fills are referenced, not reproduced.

**Short answer.** No live execution cost has ever been measured, because the bot has never traded live. The only
measured numbers are paper fills on Alpaca's simulator: three one-share market-order probes in September–October
2026 paid 0.08 bps (SPY), 1.6 bps (AAPL) and 1.7 bps (MSFT) of spread per round trip, plus $0.07 of fee minimums
that came to about 23 bps on $30 of notional. The weekly calibration and yardstick jobs that measure every paper
fill against its decision price have run on the host since 2026-11-08; their tables are on the host and were not
read here. Everything else, the 5 / 10 / 25 bps brackets, the published volatility-scaled model, and the sweep's
11 and 25 bps a side, is an assumption with a documented source. Nothing below justifies replacing the stress
case with a cheaper number.

## 1. The three layers

| Layer | What it is | Where it comes from | Status |
|---|---|---|---|
| **Live** | Fills on a real brokerage account against the quote at decision time and at order arrival | none | **Never measured.** `ALLOW_LIVE_TRADING` has never been on; there is no live fill. |
| **Paper** | Alpaca paper fills against the decision price the cycle reserved at | `StockPaperFill` joined to `StockPaperOrder` (`paper_cost_calibration.py`), probe round trips (`cost_probes.py`), the realised-cost table (`yardstick.realized_cost_table`) | Measured on the host since 2026-11-08 (Saturday jobs); three hand-run probes measured 2026-09-29 → 10-02 (`paper-results-audit-20261027.md`). The local database has no fills. |
| **Backtest** | A per-side cost charged by the replay engines | `BacktestConfig` / `BarrierConfig` (`backtester.py`, `triple_barrier.py`): 5 bps floor + 5% of causal daily volatility + square-root impact on a $10k order, plus 1 bp fee a side; the yardstick's 5 / 10 / 25 bps round-trip brackets; the sweep's flat 11 and 25 bps a side | Assumed. |

## 2. Every cost number in use, with its source

| Number | Per | Used by | Source | Kind |
|---|---|---|---|---|
| 0.08 / 1.6 / 1.7 bps | round trip, SPY / AAPL / MSFT | `paper-results-audit-20261027.md`, feasibility report Q1 | Three `qa-*` market-order probes of ~$10 each, 2026-09-29, 09-30, 10-02, realised gross −$0.0035 | **Measured, paper**, n = 3, spread only |
| ~23 bps | round trip on ~$30 notional | same audit | REG/CAT/TAF fees rounded up to the $0.01 minimum per fee type: $0.07 on $30 | **Measured, paper**; a fixed-dollar floor, so it is 23 bps on $30 and 0.07 bps on $10,000 |
| 5 + 0.05 σ + impact, +1 bp fee, a side | side | every backtest, label, gate replay and experiment-engine run | `backtester.BacktestConfig` defaults, documented in `backtest-realism-20261027.md` | Assumed (published model). About 27 bps a round trip on the ETFs and 33 on the stocks at their 2015–2026 volatility (feasibility report, sector-reversal report) |
| 5 / 10 / 25 bps | round trip | yardstick bracket on every verdict (`yardstick.cost_bracket`), short-hold veto (10 bps until measured) | `Retail bot edge feasibility.md` Q1: 5–10 bps all-in base case, 25 bps stress | Assumed, from the literature below |
| 11 bps a side (22 round trip) | side | defensive-sleeve and ETF-rotation checks, the local sweep | "about what the published model charges on these ETFs: 5 bps + 5% of a daily volatility near 1.1% + 1 bp" (feasibility report, section on the ETF check) | Assumed; a restatement of the model at ETF volatility |
| 25 bps a side (50 round trip) | side | defensive-sleeve check, the local sweep | the same reports' stress case | Assumed; twice the yardstick's stress bracket when read per round trip |
| 7–46 bps | round trip | feasibility report | Schwarz, Barber, Huang, Jorion, Odean (JF 2025): 85,000 simultaneous retail market orders at five brokers; Alpaca not in the sample | External measurement, other brokers, 2021–22 |
| 0.2–0.8 bps SPY/QQQ, 1–2 bps megacaps, 4–10 bps average S&P 500 name | round trip, spread after ~45% price improvement | feasibility report Q1 | SSGA, ETF.com, Dyhrberg–Shkilko–Werner, NYSE | External, spread only, not Alpaca |
| 0.5–3.0 × | multiplier on modelled slippage | `gate_replay.simulate` and the experiment engine once a cell has 20 fills | `paper_cost_calibration.py`: ratio of mean realised to mean modelled slippage per (symbol class × time bucket), shrunk toward 1 with 20 pseudo-fills, capped to [0.5, 3.0] | Measured on paper, then shrunk and floored; **inherits paper's biases** |

## 3. What the paper measurement actually measures

The calibration (`paper_cost_calibration.py`) and the yardstick's realised-cost table score each fill as
`slippage_bps(fill, decision price, side)` plus the reported fee, where the decision price is the price the cycle
reserved the order at (`StockPaperOrder.limit_price`), bucketed by symbol class (index ETF, sector ETF, single
stock), order kind (market, limit entry, bracket take-profit, protective stop) and time of day (open, midday,
close). Four properties of the venue and the order type decide what those numbers can and cannot say:

1. **Scheduled entries are limit orders at the decision price** (`stock_paper_ledger.reserve_stock_paper_order`,
   `order_type="market" if source == PROBE_SOURCE else "limit"`). A limit at the reference price cannot fill worse
   than it, so its measured slippage is ≤ 0 by construction. The cost of a limit entry is the fills it does *not*
   get: the order fills when the price comes back to the decision price and not when it runs away, which is
   adverse selection, not slippage. Neither the calibration nor the yardstick records the share of limit entries
   that expired or were cancelled unfilled, so **the fill rate of scheduled entries is not measured**.
2. **Probes are market orders** (one share, 10:30–14:00 ET, sold 30 minutes later; rotation SPY, QQQ, IWM, DIA, XLK,
   XLF, AAPL, MSFT, AMZN, GOOGL; three a week), scored against the 1-minute close they were decided on. They
   measure the spread the simulator charges plus the decision-to-submission latency of the bot's own loop. They do
   not measure what a limit entry costs, nor what the bot's strategies pay, because strategies do not use market
   orders.
3. **Alpaca's paper simulator fills at the NBBO**: buys at the ask, sells at the bid, with no price improvement, no
   market impact, no latency slippage and no queue position; auction orders are not treated as auction orders
   (feasibility report, citing Alpaca's forum and support pages, and the Alpaca paper-trading documentation). So
   paper overstates the spread paid by a live retail order (which gets 44–47% of the quoted spread back at a
   wholesaler) and understates everything that happens between the decision and the fill on a live venue.
4. **Overnight gap is not execution cost.** Every daily-bar strategy decides on the close and fills at the next
   open; the move from that close to that open is inside the measured "slippage against the decision price" for
   any order reserved at the close, but it is market risk, not execution. The calibration measures against the
   reserved price (the next-open reference where the cycle reserves at the open), so this separation depends on
   when the cycle reserved; the audit flagged one case where an entry was booked at a price the signal never saw
   (−1,571 bps, `paper-results-audit-20261027.md`), which is a data-plumbing error, not a cost.

The realised-cost table carries its own caveat in code: "Paper fills at the NBBO with no price improvement,
impact or auction, so these are a paper measurement, not a live one."

## 4. What is still not measured, and what would measure it

| Gap | Why it matters | What would close it |
|---|---|---|
| Live fills | The only number that decides whether a candidate pays; the literature range for Alpaca-type retail flow is 7–46 bps a round trip and Alpaca's own position in it is unknown | A live account trading one share of the probe rotation under the same probe loop, with the NBBO midpoint at decision time and at order arrival recorded for every order; until then every verdict stays bracketed |
| Fill rate of limit entries | A limit at the decision price measures ≤ 0 slippage but may not fill; the unfilled share is the real cost | Count `StockPaperOrder` rows with `order_type == "limit"` by final status (filled / cancelled / expired) per symbol class, and the mark-to-market move of the unfilled ones from reservation to cancellation |
| Quote at decision and at arrival | Separates the bot's own latency from the venue's | Store the NBBO (or the 1-minute bar) at `submission_attempted_at` next to the one at reservation; the probe loop already stores the decided-on minute close |
| Open vs midday cost | Every replay fills at the open, the dearest half hour; the calibration's `open` cell only fits once 20 fills land there | Keep the probes, add one probe a week in the first 30 minutes |
| Fee floor at one share | $0.01 minimums make a $30 round trip cost 23 bps and a $500 one about 1 bp | Report fees in dollars next to bps on every one-share probe; never scale them as bps |
| Dividends and corporate actions on held positions | Not a cost, but a P&L line the paper account books differently from the adjusted bars | `dividend_check.py` already reports booked / none_found / unavailable weekly |

## 5. How the sweep's scenarios relate to the measurements

| Scenario | Per side | Per round trip | Relation to measured and assumed numbers |
|---|---|---|---|
| Sweep, base | 11 | 22 | ≈ the published model on the ETFs; 2× the yardstick's 10 bps bracket; 10–100× the three probes' spread |
| Sweep, stress | 25 | 50 | 2× the yardstick's 25 bps stress bracket; at the top of the Schwarz range |
| Yardstick base / stress | 2.5 / 12.5 | 5 / 25 | the feasibility report's planning numbers |
| Published model | 5 + 0.05 σ + impact + 1 | ≈ 27 ETFs, ≈ 33 stocks | what every engine replay charges unless a calibrated cell exists |
| Paper probes | — | 0.08–1.7 spread (+ fee floor) | measured, n = 3, market orders, simulator |

Both sweep scenarios stay in the report. The 11 bps case is a model restatement, not a measurement, and the 25
bps case is a stress case; neither should be replaced by the probes' 0.1–1.7 bps, because those are a simulator's
spread on market orders with no fill-rate, latency or auction component.

## 6. Reading the host's measured tables (one request each, no replay)

The production tables are the only measured paper costs beyond the three probes. They are read with a viewer key;
nothing below runs a backtest or writes:

```bash
curl -s -H "X-API-Key: $AUTH_VIEWER_KEY" https://frozenstock.replit.app/stock-paper/cost-calibration | jq '.tables.overall, .tables.by_symbol_class, .tables.by_order_kind, .tables.by_time_bucket, .cells'
```

```bash
curl -s -H "X-API-Key: $AUTH_VIEWER_KEY" https://frozenstock.replit.app/stock-paper/yardstick | jq '.realized_costs'
```

What to record from them into the next revision of this note: `tables.overall.residual` (1.0 means the model is
right on average), fills and priced fills per class, mean / median / p90 realised per-side cost per class and per
order kind, the probes' round trips per symbol, and the date window. Add the fill-rate count from section 4 once it
exists. Until a class has 20 priced fills, the yardstick's veto keeps using 10 bps a round trip and every verdict
keeps its bracket.
