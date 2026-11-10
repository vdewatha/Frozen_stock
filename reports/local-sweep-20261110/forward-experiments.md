# Frozen forward experiments from the local sweep: 2026-11-10

Pre-registration for the six candidates named in `reports/local-sweep-20261110.md`, section 7. Everything in this
file and in `forward-experiments.json` (SHA-256 `0a87603dfa1ba05343d47a000484c2b3ee310d1203c9f0cde814038e1a84b439`)
is fixed before any evaluation sample is collected. A change to a rule, universe, exit, cost, benchmark or
threshold after this date is a new experiment with a new trial count, not a revision of this one. Paper only;
nothing here sizes, places or authorises an order.

## Status of the search

Strategy discovery's weekly mutation step stays **paused** while these run (`strategy_discovery_mutation_enabled`
off). The library's own weekly re-grading is harmless (same hashes, no new trials) and may continue. No new
rule, gate, lookback or universe is to be tested on history against these candidates; the trial count the sweep
charged them (3,104 configured, 3,086 with trades) is the count they carry forward.

## Common terms, fixed

| Term | Value |
|---|---|
| Execution | Decision on the close of bar t, fill at the next open; stop first inside a bar; gaps fill at the open; indicator exit at the next open; one position per symbol; long only (the app's `triple_barrier.label_events` semantics, which the paper path mirrors) |
| Exits | 10% catastrophe stop, no target, 10-session time stop, indicator exit per rule (`SHORT_TERM_REVERSION_EXIT_POLICY`) |
| Universe | As listed per candidate; today's 45-symbol paper list, never widened during the test |
| Gate readings | `vix_above_20`: CBOE VIX close dated on the decision bar ≥ 20, fails open when missing for 5 sessions; `spy_sma50`: SPY's last completed close above the mean of its last 50 completed closes. Both are recorded on each paper signal's features at decision time |
| Cost brackets reported on every review | 10, 22, 25 and 50 bps a round trip, plus the published model, plus the host's measured class cost once a class has 20 priced fills. The *acting* cost for the verdict is **25 bps a round trip** (the yardstick's stress bracket), replaced by the measured class cost only if the measured cost is higher |
| Benchmarks | (a) the same symbol's drift over the same hold, measured causally (mean daily return up to the decision bar × holding days); (b) the volatility-matched SPY/T-bill arm of `yardstick.passive_scores` on the candidate's daily equal-weight return; (c) for the book, the passive VOO/SGOV arm |
| Statistics | Month-clustered t of per-trade net return and of excess over drift; the e-process judge of `verdict_stats.py` recorded in shadow; portfolio max drawdown and capital in use on the equal-weight daily series |
| Review schedule | Quarterly from the first paper trade (first review no earlier than 2027-02-10), and at the checkpoint and the verdict sample below. Each review is a table appended to this file, never an edit of what is above it |
| Promotion criteria (all must hold at the acting cost, at the verdict sample) | net expectancy > 0 with clustered t ≥ 2; excess over drift > 0 with t ≥ 2; passes the yardstick's excess check (t ≥ 2 on both the passive-arm excess and the SPY residual); positive in ≥ 60% of completed quarters; max drawdown of the equal-weight series ≤ 15%; the short-hold veto's gross ≥ max(30 bps, 3 × measured round trip). A candidate that meets these is eligible for the normal forward-trial promotion gates, not for live |
| Stop rule | A candidate is closed early if, after at least 60 trades, its clustered t of net return at the acting cost is ≤ −2, or the equal-weight series draws down more than 15% |

## Sample sizes (fixed)

Inputs: per-trade net standard deviation 270 bps for the reversion exits (`verdict-inputs-20261107.md`, RSI(2) and
IBS, 266–272); same-date residual correlation ρ = 0.07 trade-weighted, 0.15–0.18 for reversion
(`liquid-universe-20261107.md`, `verdict-inputs-20261107.md`); smallest economically worthwhile edge 30 bps a trade
net at the acting cost (the yardstick floor). For a one-sided t = 2 on the mean, n = (2 × 270 / 30)² ≈ 324
independent trades. With m trades sharing a decision date on average and ρ = 0.15, the design effect
1 + (m − 1)ρ is 1.0 (one symbol a day), 1.6 (five) and 2.5 (eleven). Rounded:

| Candidate | Rule | Universe (symbols) | Expected trades a year | Design effect | Verdict sample | Checkpoint | Years to verdict |
|---|---|---|---|---|---|---|---|
| 1 | `c8e129d06c` close > SMA200 and 5-day return < −5%; exit close > SMA5 | sector ETFs (12) | ~17 | 2.5 | 800 | 100 | ~45 |
| 2 | `1cd460d679` close > SMA200 and 5-day return < −8%; exit first up close | single stocks (29) | ~45 | 2.5 | 800 | 100 | ~18 |
| 3 | `213239cdf2` 5-day return < −5%; exit close > SMA5; SPY > SMA50 | sector ETFs (12) | ~12 | 2.5 | 800 | 100 | ~65 |
| 4 | `4ec6c68526` close > SMA200, vol20/vol100 < 1, RSI(2) < 10; exit close > SMA5; VIX ≥ 20 | index ETFs (4) | ~9 | 1.5 | 500 | 100 | ~55 |
| 5 | `7df4090f31` 4 consecutive down closes; exit close > SMA5; VIX ≥ 20 | single stocks (29) | ~79 | 2.5 | 800 | 100 | ~10 |
| 6 | IBS < 0.1, exit IBS > 0.8 (the bot's rule), tagged by VIX ≥ 20 at the decision close | all 45 | ~370 gated (~900 ungated) | 2.5 | 800 gated | 100 | ~2 |

Reading: **only candidate 6 can reach a verdict inside the project's horizon.** Candidates 1–5 are registered so
their forward record accumulates honestly in shadow and so that no one re-tests them on history; they are not
expected to deliver a verdict, and the quarterly reviews will say "inconclusive, n = …" until they do. Pooling 1–3
(one family, three variants) into one hypothesis is allowed and was decided here, before data: the pooled
verdict uses the pooled trades and counts as three trials.

## Candidate 6, the one that can decide something

* **What is tested.** Whether the bot's IBS entries decided on a day with VIX ≥ 20 have a higher net expectancy at
  the acting cost than those decided with VIX < 20, and whether the gated subset alone passes the promotion
  criteria. Not whether IBS as a whole has an edge (the sweep says it does not at 25 bps: +0.3 bps, t 0.03).
* **Mechanism.** No code change is needed to start. IBS keeps trading exactly as it does; at review time each
  closed IBS trade is tagged with the VIX close dated on its decision bar (from the `^VIX` history, deterministic
  and reproducible), and the two buckets are compared. If `vix_gate` readings are already on the signal features
  they are used instead; they must agree with the history tag.
* **Pre-stated expectation from the sweep (2015–2026, 25 bps a side):** gated +42.9 bps (t 2.80, 4,368 trades),
  ungated +0.3 (t 0.03, 10,088 trades); in 2000–2014 the gated subset made **−7.5 bps**, so the hypothesis is
  that the post-2015 reading is a regime, and the prior is against it.
* **Verdict.** At 800 gated trades (or 2 years, whichever first): promote the gate for a paper forward trial only
  if the gated bucket passes every promotion criterion **and** beats the ungated bucket by ≥ 20 bps with the
  paired clustered t ≥ 2. Otherwise record the result and close.

## Candidates 1–5, registration details

For each, the exact rule in the discovery rule language, the exit plan, the symbol list and the sweep record at
both costs are in `forward-experiments.json`. They are entered as **shadow** `rule_<id>` rows (status
`paper_trading_candidate` with exploration sizing, as `strategy_discovery.admit_survivors` would create them), or
as zero-capital shadow decisions through `setup_shadow.py`, whichever the operator chooses; the choice is recorded
in the first review. Candidates 4 and 5 carry `vix_gate: "above", vix_level: 20`; candidate 3 carries
`market_gate_sma: 50`.

## What is reported at every review

1. Trades, net expectancy at every bracket, clustered t, excess over drift and its t, win rate, mean hold, mean
   dollars per trade at the size actually traded.
2. Equal-weight daily series: return, max drawdown, capital in use, and the yardstick's passive-arm excess and SPY
   residual with their t.
3. The e-process judge's reading in shadow.
4. For candidate 6: both buckets side by side and the paired difference.
5. The measured cost of the class from the host's calibration table, and whether it changed the acting cost.
6. The sample reached against the verdict sample, and the words "inconclusive" where it has not been reached.

## Registration

Registered in the paper bot on **2026-11-10**, commit `6d69727` (branch `claude/forward-experiments-wiring-5c7d38`,
`artifacts/api-server/backend/app/services/forward_experiments.py`). The bot carries a byte-identical copy of
`forward-experiments.json` (`app/services/data/forward-experiments-20261110.json`, SHA-256 verified on every load) and
records one `forward_experiment` / `register` audit row per candidate with the candidate verbatim and the terms above.
Mechanism, chosen here and recorded on each registration row: candidates 1, 2, 3 and 5 are `rule_<id>` rows with status
`paper_trading_candidate` (exploration sizing; 3 with `market_gate_sma: 50`, 5 with `vix_gate: "above", vix_level: 20`),
candidate 1 reusing the discovery explore row after a verbatim check of its rule, exits and scope; candidate 4 is a
tag-and-compare on discovery's ungated `rule_4ec6c68526` row (its trades decided with `vix_above_20` true are exactly the
gated row's trades, without a twin row colliding on the same symbols), or a gated row where that row does not exist;
candidate 6 is a tag-and-compare on `ibs_mean_reversion`, which is unchanged. Rows in `shadow` status (IBS since the
short-horizon re-verdict) contribute their BUY decisions scored as the paper path would have traded them (next open, the
row's tested exits over stored daily bars, costs both sides); rows that place entries contribute their closed round
trips. Every paper signal now records `gate_readings` (`vix_above_20`, `spy_sma50`) at decision time; a missing VIX close
is recorded as unknown, never guessed. The mutation step is off by default (`strategy_discovery_mutation_enabled`).
Reviews are `forward_experiment` / `review` audit rows (append-only), each with the table below rendered; the Saturday
job records one when the schedule calls for it, and `GET /stock-paper/forward-experiments` shows the live reading at any
time. First scheduled review: 91 days after the first decision bar and no earlier than **2027-02-10**.

## Reviews

_None yet._
