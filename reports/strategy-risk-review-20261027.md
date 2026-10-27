# Strategy, sizing and risk review (2026-10-27)

Branch: `improve/strategy-risk`. Scope: `app/services/strategies/`, `trade_candidates.py`,
`opportunity_radar.py`, `market_regime.py`, `probabilistic_model.py`, `feature_pipeline.py`,
`portfolio_allocation.py`, `portfolio_risk.py`, `risk.py`, `risk_actions.py`, `risk_settings.py`,
`safety_controls.py`, `news_sentiment.py`.

No network calls were made, no orders placed, `.env` was not read. Every change either tightens
a gate or corrects a defect. No safety gate, kill switch, live-trading guard or promotion
requirement was loosened. No DB migration is needed; new fields are additive JSON keys and
optional response-schema fields.

Regression tests: `tests/test_strategy_risk_review.py` (41 tests). Run against the pre-change tree,
35 fail and 6 pass. The 6 that pass on both trees are regression guards for paths that were
already safe by accident: three warm-up HOLD cases, ordinary approval and valid settings updates.

Line numbers below refer to the pre-change files (commit `719dffb`).

## Defects found (ranked by severity)

### High

1. **NaN confidence and drawdowns passed the risk gate.** `risk.py:67-72`. `float(nan) < min_confidence`
   and `nan > max_drawdown` are both `False`, so `approve_trade` approved a BUY with NaN
   confidence or an unknown (NaN) daily or strategy drawdown. Every caller (paper trading, the stock
   paper ledger and `/risk/approve`) inherits this. **Fixed:** non-finite inputs are rejected
   (fail closed).
2. **NaN clamped to 100% confidence in every strategy.** `strategies/base.py:32-33`. `clamp` used
   `max(lo, min(hi, x))`. Python's `min(1.0, nan)` returns `1.0`, so any NaN indicator (warm-up
   window, zero divisor) became confidence 1.0 or probability 1.0. Example: `moving_average_crossover`
   on 10 bars returned confidence 1.0, and the `ensemble` emitted **BUY at 0.68 confidence on 10 flat
   bars**. **Fixed:** NaN or inf maps to the neutral 0.5, and every strategy now returns a neutral
   HOLD until its longest window has warmed up.
3. **Flat prices read as maximally oversold.** `technical.py:41-44, 201-204, 281-284`. RSI used
   `loss.replace(0, 1e-9)`, so a window with no gains and no losses gave RSI 0. A halted, stale or
   forward-filled symbol produced `rsi_mean_reversion` BUY at confidence 1.0, and `bollinger_mean_reversion`
   produced BUY at 1.0 because a zero-width band also put close at or below the lower band.
   **Fixed:** a shared `rsi()` returns 50 for a flat window and 100 for gains only. Bollinger
   returns HOLD on a zero-width band.
4. **No aggregate (gross) exposure cap in the pre-trade gate.** `risk.py:79-82`. Only the symbol's
   *existing* exposure was compared to `max_symbol_exposure`. `max_total_exposure` existed in
   `DEFAULT_RISK_RULES` but `approve_trade` never checked it, so the book could exceed 100% of equity
   (a test portfolio at 139% gross was approved). The size of the proposed order was also ignored.
   **Fixed:** `approve_trade` now blocks at or above the gross cap. When a caller supplies the
   optional `signal["proposed_exposure"]`, it blocks if existing + proposed exposure would breach
   the symbol cap or the gross cap.
5. **Market-regime filter compared against a label that is never produced.** `market_regime.py:71,75`.
   `_rate_proxy_label` emits `risk_on`, `risk_off` or `neutral`, but the classifier checked
   `rate_regime != "restrictive"` / `== "restrictive"`. As a result, a 10% 60-day sell-off inside a
   longer uptrend was still labelled `bull_trend`, and `policy_stress` could never be reached.
   **Fixed:** the classifier now checks `risk_off`.

### Medium

6. **The allocation plan ignored gross exposure.** `portfolio_allocation.py:183,188-189`. Each "add"
   was gated only on per-symbol room, and the targets were `symbol_limit x share` with no
   volatility or regime awareness. **Fixed:** room is now `min(symbol room, gross room)`, and the
   per-symbol cap is shrunk by the ATR volatility cap and the regime multiplier (see
   Improvements). The row's `sizing_context` explains the cap.
7. **The allocation executor could add to the same symbol twice in one pass.** `portfolio_allocation.py:665-680`.
   Symbol room is computed once per plan, so two strategies on one symbol could each "add" against
   the same room. **Fixed:** only one add per symbol per pass; the second is skipped with a reason
   and waits for a fresh plan.
8. **The MACD confidence floor sat exactly on the entry gate.** `technical.py:79`. Confidence was
   `0.55 + ...`, so any non-zero histogram, even noise-sized, met the 0.55 threshold used by the
   backtester and the scanner, and confidence carried no information there. **Fixed:** the baseline
   is 0.5, and a zero histogram is HOLD. This changes which MACD entries are taken: only histograms
   of at least about 0.25% of price now reach 0.55.
9. **The scanner's confidence bar was below the execution gate.** `trade_candidates.py:105`. The
   scanner used `confidence >= 0.55`, while `approve_trade` requires `min_confidence` (0.58). Rows
   between 0.55 and 0.58 were labelled `positive_candidate` but were always blocked at execution.
   **Fixed:** `MIN_STRATEGY_CONFIDENCE = max(0.55, DEFAULT_RISK_RULES["min_confidence"])`.
10. **The portfolio monitor did not alert on gross exposure.** `portfolio_risk.py:82-118`. **Fixed:**
    it now raises a warning at 85% and a breach at 100% of `max_total_exposure`, and adds
    `max_total_exposure` and `max_risk_per_trade` to `risk_limits`. Through `risk_actions`, a
    *persistent* "Gross exposure" breach now pauses strategies, as other non-kill-switch breaches
    already did.
11. **Synthetic news counted as evidence.** `news_sentiment.py:290-295` and `opportunity_radar.py:59`.
    `summarize_news_context` auto-seeds templated `mock_news` headlines, and provider failures fall
    back to them too. Their tone then fed `opportunity_score`. **Fixed:** the summary carries
    `synthetic_only`, and the radar ignores sentiment when it is true.
12. **Drawdown exactly at the limit was allowed.** `risk.py:69,71` used `>` while
    `portfolio_risk.py:97` alerts at `>=`. **Fixed:** the gate now blocks at `>=`. Note that a
    configured limit of 0 now blocks all entries.

### Low

13. **Warm-up of the trend filter.** `technical.py:279-280`. The 100-EMA (`adjust=False`) was used
    from bar 1. The backtester starts calling strategies at bar 61, so `trend_pullback` traded
    against an EMA dominated by its seed price. **Fixed:** the strategy requires `max(fast, slow, 15)`
    rows.
14. **MA crossover tie → SELL** (`technical.py:22`); it is now HOLD. A **zero-volume breakout** fell back
    to a ratio of 1x (`technical.py:249`); it is now HOLD with an explicit reason.
15. **The service layer did not validate risk settings.** `risk_settings.py:55-57`. The API schema
    bounds values, but `update_risk_settings` stored anything, such as NaN (which silently disables
    every comparison in `approve_trade`), booleans or out-of-range values. **Fixed:** it validates
    against the same bounds as `RiskSettingsUpdateRequest` and rejects the whole update atomically.
16. **Off-by-one error in the radar's 20-day and 60-day returns.** `opportunity_radar.py:26-27`. The code
    required `len > 21` while reading `iloc[-21]`, so the exactly-21-row case returned 0. **Fixed:** it now uses `>=`.

## Improvements made

- **Cost-adjusted expected edge** (`trade_candidates.py`). The round-trip cost is derived from
  `BacktestConfig` (2 x (fees + slippage) = 12 bps). A model horizon qualifies only if
  `p_up >= 0.54`, gross ER > 0.2%, **and** net ER (after costs) >= 0.1%. Horizons are ranked by
  net edge. Non-finite predictions never qualify.
- **ATR volatility sizing.** `average_true_range_pct()` (14-bar, using high, low and previous
  close) is computed for each symbol. `risk.volatility_capped_exposure(atr_pct)` returns the largest
  equity fraction whose 2xATR stop loses at most `max_risk_per_trade`. The allocation plan caps
  each symbol's target with it, and it only ever shrinks the configured cap.
- **Regime risk filter.** `market_regime.regime_risk_multiplier()` uses the most restrictive of
  these factors:
  - `bear_stress` 0.25
  - `policy_stress` and `volatile_range` 0.5
  - SPY downtrend 0.5
  - high volatility 0.5
  - 60-day `risk_off` 0.75

  Otherwise the multiplier is 1.0. The scanner records it and the allocation plan applies it to
  targets. Because targets shrink, open positions above the new target can be *trimmed*
  (reduce-only).
- **Structured decision "why".** Every scanner candidate carries a versioned `decision_trace`:
  gate booleans, thresholds, inputs (strategy action and confidence, model horizon and
  probability, backtest score and rejections), expected edge (gross, cost, net) and risk (ATR,
  regime multiplier and reasons). It is persisted in the existing `TradeCandidateSnapshot.payload`
  JSON, so no migration is needed. It is exposed through optional fields on the `TradeCandidate`
  response schema.

## Files changed

- `app/services/strategies/base.py`: NaN-safe `clamp`, `neutral_signal`, `has_valid_history`, and a shared `rsi`.
- `app/services/strategies/technical.py`: warm-up guards, flat-market RSI, MACD and MA neutrality, and volume confirmation.
- `app/services/risk.py`: fail-closed `approve_trade`, gross and proposed exposure caps, and `volatility_capped_exposure`.
- `app/services/market_regime.py`: label fix and `regime_risk_multiplier`.
- `app/services/trade_candidates.py`: cost-adjusted edge, ATR, regime multiplier, `decision_trace`, and the aligned confidence bar.
- `app/services/portfolio_allocation.py`: gross room, volatility and regime capped targets, and one add per symbol per pass.
- `app/services/portfolio_risk.py`: gross exposure alert and extra `risk_limits` keys.
- `app/services/news_sentiment.py` and `app/services/opportunity_radar.py`: the `synthetic_only` flag and the off-by-one fix.
- `app/services/risk_settings.py`: service-level validation.
- `app/schemas/trading.py`: optional `TradeCandidate` fields (`net_expected_return`, `round_trip_cost`, `atr_pct`, `volatility_exposure_cap`, `regime_risk_multiplier`, `decision_trace`).
- `tests/test_strategy_risk_review.py`: new.

## Deliberately left alone

- **`probabilistic_model.py` and `feature_pipeline.py` (feature version 1).** In v1, RSI (0-100) and
  the raw MACD histogram are globally clipped to [-5, 5], so `rsi_14` is about 5 almost always, a dead
  feature. Also, the "comparable" k-NN `expected_return` uses unscaled L1 distance (dominated by
  `volume_change`), and `_best_positive_prediction` compares expected returns across 1, 5 and 20-day
  horizons without normalising per day, which favours long horizons. Feature version 2 already
  fixes the scaling. Changing v1 would change `feature_config_id` and invalidate trained or
  registered models, so it belongs to the learning-loop owner. The model is fit in-sample, so its
  probabilities are not calibrated. Labels and features themselves are causal: no look-ahead was
  found.
- **Sector and correlation caps.** `Asset.sector` exists, but no caller supplies sector exposure
  to the risk gate, and candidates do not carry sector. Adding dead code was not justified.
  Recommended next step: pass the sector into `approve_trade` and into the allocation symbol caps.
- **Unknown regime stays at 1.0x.** The scanner calls `latest_market_regime(auto_detect=False)`.
  Shrinking size whenever detection simply has not run would be a silent behaviour change.
- **`safety_controls.py` and `risk_actions.py`.** These were reviewed and no defects were found.
  The kill switch and pause paths are unchanged. `resume_candidate_strategies` returns paused
  strategies to `paper_trading_candidate`, not active, which is correct.
- **The `/risk/approve` route** uses a hard-coded demo `PortfolioState`. It is outside scope and
  informational only.

## Found in other owners' files (reported, not fixed)

- **`backtester.py:78-85` (backtester owner).** Stop-loss and take-profit are evaluated only at each bar's
  **open**, against entry. Intrabar lows are ignored and a gap through the stop fills at the open,
  so realized losses can be much wider than `stop_loss_pct`, and an intraday stop touch that
  recovers by the next open is never triggered. A position also cannot exit on its own entry bar.
  Use `low` and `high` with gap-aware fills.
- **`backtester.py:122-140`.** Open positions are force-closed at the last **close**, while every
  other fill uses the next open.
- **`paper_trading.py:81-96` (outside every listed front).** `_portfolio_state` never sets
  `daily_drawdown`, so it is always 0 and the daily drawdown gate cannot fire on this path.
  Exposures also use entry price, not mark. `_position_size` (`paper_trading.py:99`) uses a fixed
  3% stop (`STOP_LOSS_PCT`) instead of volatility. `risk.volatility_capped_exposure` is available
  to replace it.
- **`stock_paper_ledger.py:1870-1880`.** Pending (non-terminal) orders are added to the strategy and
  position counts but **not** to `open_positions_by_symbol`. A symbol with a pending buy is
  therefore under-counted against the symbol cap and the new gross cap. Recommended: add pending
  notional / equity for each symbol, and pass `proposed_exposure=quantity*price/equity` to
  `approve_trade`.
- **`news_sentiment.py:160`** (in scope, left as is). The `url = url or X if cond else url`
  precedence is easy to misread but evaluates correctly. "risk" in "risk appetite" scores
  negative. This is a cosmetic issue only now that synthetic news is excluded.

## Test results

See the commit message and the final summary for the full-suite counts at the time of commit.
