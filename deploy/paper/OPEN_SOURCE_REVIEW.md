# Open-source integration review

Reviewed 2026-10-10. This is a targeted repository/documentation survey and an
implementation review of the two adopted adapters, not a security audit of every
project. Stars, README backtests, and agent debates are not evidence of profit.
No external agent was given our credentials and no repository's setup scripts
were executed. Adopted Python packages are version-pinned in requirements.txt.

## 2026-10-16 current comparison

This follow-up compared the current repository with the primary repositories for
[QuantConnect LEAN](https://github.com/QuantConnect/Lean),
[NautilusTrader](https://github.com/nautechsystems/nautilus_trader),
[Freqtrade](https://github.com/freqtrade/freqtrade),
[Qlib](https://github.com/microsoft/qlib),
[TradingAgents](https://github.com/TauricResearch/TradingAgents), and
[vectorbt](https://github.com/polakowo/vectorbt). No source was copied, no
installation scripts were executed, and no broker credentials or order APIs
were used.

### What this codebase already has

The current design is closer to a guarded research control plane than to a
single autonomous trading bot:

- `research_simulator.py` makes next-open timing, fees, spread, slippage,
  participation, partial fills, and drawdown explicit.
- The IEX research lane stores source-labelled bars and per-symbol forecasts;
  synthetic data is excluded from trusted execution evidence.
- Online learning uses predict-before-target-reveal, bounded replay, immutable
  prediction records, and separate symbol models.
- Celery jobs and the hosted direct loops use Redis leases, while paper/live
  eligibility remains behind reconciliation, lineage, risk, and approval gates.
- `live_broker.py` treats uncertain broker outcomes, missing commissions, stale
  data, and reconciliation gaps as blocking conditions.

These boundaries are more important than adding more workers. More workers can
increase throughput, but they also increase duplicate work, API pressure,
selection bias, and shared-portfolio correlation risk. Worker count should be
chosen from measured queue latency and provider rate limits, not from the number
of strategies one hopes to run.

### Reusable ideas and decision matrix

| Project | Safe idea to reuse | Fit here | Decision and risk |
| --- | --- | --- | --- |
| LEAN | Separate fee, slippage, fill, and buying-power models; keep the same strategy contract across backtest and paper contexts. The upstream brokerage model is explicitly Apache-2.0 licensed. | High for research simulation and contract tests. | Adopt the design pattern only. Do not replace the Python API with the .NET engine until data normalization, adapter behavior, and operational recovery are independently validated. Apache-2.0 notices and any dependency notices still need to be preserved. |
| NautilusTrader | One event-driven domain model across backtest, sandbox, and live, plus explicit command/event timing and execution reconciliation. | High for a future isolated event-tape simulator; the current broker state machine already covers part of this. | Do not import now. The project is LGPL-3.0 and has a separate trademark policy requiring careful attribution and non-affiliation language for distributed integrations. A full adoption would also add a Rust/Python runtime boundary. |
| Freqtrade/FreqAI | Dry-run-first operations and configurable protections such as stoploss guard, max drawdown, cooldown, and low-profit locks. Its lookahead-analysis documentation is also a useful test pattern. | High as research eligibility checks and anti-lookahead tests; low as a stock execution replacement. | Do not copy GPL-3.0 source into this service without a deliberate distribution decision. Keep the existing stock and crypto lanes separate. Reimplement only the documented behavior with tests if needed. |
| Qlib | Point-in-time data handling, recorded experiment stages, dataset snapshots, signal analysis, and portfolio backtest as separate artifacts. | High for strengthening dataset lineage and experiment reproducibility. | Prefer a small local artifact contract over adding Qlib. Qlib is MIT, but its data providers and downloaded datasets have separate terms, and its data format would add migration and maintenance cost. |
| TradingAgents | Role-separated research opinions and timestamped evidence before a final synthesis. | Medium for explainable research only. | Keep agents outside signal authority and execution. The repository is Apache-2.0, but LLM calls, tool outputs, prompt injection, stale news, and cost/latency are operational risks. Require structured evidence and deterministic replay before accepting any agent output. |
| vectorbt | Matrix/vectorized parameter sweeps and portfolio statistics for offline screening. | Medium for a separately packaged research benchmark. | Do not add as a runtime dependency: the upstream community license is Apache-2.0 with Commons Clause, not plain Apache-2.0, and its restriction may conflict with a hosted product. Avoid using large sweeps as proof of generalization. |

### Recommended implementation order

1. Add a versioned research-run artifact contract containing dataset snapshot
   digest, feature-availability cutoff, train/validation/holdout bounds,
   strategy version, cost model, and result digest. This captures Qlib's useful
   reproducibility boundary without importing Qlib.
2. Add an optional, research-only event-tape mode to the existing simulator:
   explicit event timestamps, decision latency, venue receipt time, partial
   fill events, and unknown outcome states. Keep next-open mode as the stable
   baseline and compare both modes rather than silently changing results.
3. Add property/regression tests for Freqtrade-style protections and LEAN-style
   transaction components: cooldown after exit, drawdown lock, max
   participation, fee/spread/slippage accounting, and no future-data access.
4. Keep per-symbol/scalp workers as bounded leases over a shared portfolio
   risk service. Record queue age, provider response age, duplicate skips, and
   failure class before increasing concurrency.
5. Use LLM or multi-agent research only to propose hypotheses and explain
   evidence. A proposal must enter the same immutable backtest, holdout, and
   paper-evidence path as a human-authored strategy; it must never toggle a
   kill switch or submit an order.

### Explicit non-goals

This survey does not establish profitability, authorize paper or live orders,
or make a synthetic/one-venue IEX series equivalent to an executable NBBO
feed. It also does not recommend copying strategy code from GitHub. The
current safety gates, kill switch, paper-only deployment flags, reconciliation,
and separate approval boundary remain in force.

## 2026-10-10 intraday/backtest follow-up

The current backtester was compared with the upstream workflows in [Microsoft
Qlib](https://github.com/microsoft/qlib), its [high-frequency workflow
example](https://github.com/microsoft/qlib/blob/main/examples/highfreq/workflow.py),
and [QuantConnect LEAN's custom transaction-model
example](https://github.com/QuantConnect/Lean/blob/master/Algorithm.CSharp/CustomModelsAlgorithm.cs).
Qlib's workflow keeps dataset fitting, test periods, signal analysis and
portfolio backtest as separate recorded stages, while its high-frequency example
keeps a distinct backtest dataset handler. LEAN models fees, slippage, fills and
buying power as explicit components. These are useful design references, not
drop-in dependencies for this repository.

Two concrete, research-only improvements are recommended:

1. **Expose a matched passive benchmark from `backtester.run_backtest`.** Return
   cash and buy-and-hold results using the same evaluated dates, next-open start,
   fees and slippage, plus strategy excess return. The separate
   `research_simulator` already does this; making the primary backtest response
   carry the same comparison would prevent a positive raw return from being
   mistaken for market outperformance. This should be additive and diagnostic:
   it must not alter the score, promotion decision, or live-trading settings
   until its contract has dedicated tests and a schema review.

2. **Add an opt-in intraday reference-fill mode behind the offline research
   simulator.** Preserve the current deterministic next-open mode, but allow a
   research-only configuration to model explicit spread, volume participation,
   partial fills, and high/low stop-trigger ordering. This follows LEAN's
   separation of fee, slippage, fill and buying-power models while keeping the
   existing primary backtester stable. Any intraday mode must label ambiguous
   OHLC trigger ordering as unknown or use a documented conservative rule; it
   must never be used as evidence that a broker would have filled an order.

Do not install Qlib or LEAN as part of this follow-up. Qlib's high-frequency
example requires a separate data format/handler, and LEAN brings a .NET engine,
data normalization and brokerage-model surface. Both would enlarge the trusted
research surface before the two smaller improvements are validated locally.

## Decisions

| Project | License reported upstream | Fit and decision |
| --- | --- | --- |
| [alpaca-py](https://github.com/alpacahq/alpaca-py) | Apache-2.0 | Adopt 0.44.0 for authenticated IEX research data. Reuse request types/transport; add explicit timeout, no redirects, four-page ceiling, strict validation and redacted failures. No TradingClient/order path in this adapter. |
| [exchange_calendars](https://github.com/gerrymanoim/exchange_calendars) | Apache-2.0 | Adopt 4.13.2. Replace handwritten NYSE holidays/session bounds, including exceptional closures, early closes and DST. Pin updates and rerun calendar tests when schedules change. |
| [yfinance](https://github.com/ranaroussi/yfinance) | Apache-2.0 | Upgrade 0.2.41 to 1.7.0. All four symbols returned validated real daily history in the local probe. Retain the independent Yahoo chart fallback with its own source label; never substitute synthetic observations. Yahoo data terms are separate from the code license. |
| [Qlib](https://github.com/microsoft/qlib) | MIT | Best next research-engine candidate: model comparisons, dataset handling and rolling research. Do not replace the existing purged trainer yet. A benchmark must preserve our immutable snapshots, horizon purge, one-use holdout and baseline comparisons. Larger dependency and data-format migration than these immediate repairs. |
| [R&D-Agent](https://github.com/microsoft/RD-Agent) | MIT | Candidate for isolated experiment proposals and factor generation. Generated code needs a no-secret, no-broker sandbox and bounded compute. Not connected to execution. LLM inference is not automatically free. |
| [TradingAgents](https://github.com/TauricResearch/TradingAgents) | Apache-2.0 | Useful role decomposition and timestamped research patterns; local Ollama is supported upstream. Our current shadow lane is a bounded single-prompt implementation, not a deployment of the full upstream graph. Do not present it as many independently tested agents. Defer runtime adoption until provider and evaluation contracts are ready. |
| [FinRL](https://github.com/AI4Finance-Foundation/FinRL) | MIT | RL research reference, with [paper-trading tutorials](https://github.com/AI4Finance-Foundation/FinRL-Tutorials). Not a drop-in improvement to the current calibrated supervised model. Needs independent environment/reward validation and out-of-sample comparison before promotion. |
| [Freqtrade/FreqAI](https://github.com/freqtrade/freqtrade) | GPL-3.0 | Operational/retraining reference and existing separate crypto integration. Not a replacement for this US-equity Alpaca adapter. Keep crypto and stock evidence isolated; review license obligations before distributing combined code. |
| [LEAN](https://github.com/QuantConnect/Lean) | Apache-2.0 | Strong candidate for a separate event-driven reference backtest. A full engine replacement introduces .NET infrastructure, data normalization and broker integration work. Open-source code does not include every data subscription or hosted service. |
| [vectorbt](https://github.com/polakowo/vectorbt) | Apache-2.0 with Commons Clause | Fast research sweeps are appealing, but current [license conditions](https://github.com/polakowo/vectorbt/blob/master/LICENSE.md) are not plain Apache-2.0. Not adopted. Sweeping more variants also increases selection bias. |
| [backtesting.py](https://github.com/kernc/backtesting.py) | AGPL-3.0 | Compact reference simulator, but licensing and execution-model differences warrant a deliberate decision. Not copied or installed. |

License labels were checked against repository metadata; review the actual
licenses and dependency notices before distribution. The deployed dependencies
retain their installed upstream license metadata. We did not vendor source from
the comparison frameworks.

## Provider-contract findings

[Alpaca account activities](https://docs.alpaca.markets/us/docs/account-activities)
do not guarantee a commission on each FILL. Nontrade activities can use a date,
and fees can be separate FEE/CFEE activities. The read-only diagnostic now
distinguishes this documented shape from our stricter existing ledger contract.
It does not fabricate timestamps, infer absent fees as zero, or certify a venue.
A versioned activity-ledger migration and observed replay/cash reconciliation
remain necessary before unattended broker execution.

[Paper trading](https://docs.alpaca.markets/us/v1.4.2/docs/paper-trading) omits
important real-world effects. IEX is one exchange, while simulated fills can be
based on NBBO. The new collector is labeled `alpaca_iex` / `iex`, and cannot
satisfy `tradier` / `sip` readiness. Missing minutes remain unknown. A successful
poll, a complete HTTP pagination sequence and a complete market tape are three
different claims.

## Architecture decision

### Incremental learning follow-up

[River](https://github.com/online-ml/river) and its
[delayed progressive validation](https://riverml.xyz/latest/api/evaluate/progressive-val-score/)
provide a useful evaluation pattern: predict before the target is revealed,
evaluate that frozen prediction, then learn. We adopt the pattern, not copied
source or a new runtime dependency. The existing
[scikit-learn SGDClassifier](https://scikit-learn.org/1.5/modules/generated/sklearn.linear_model.SGDClassifier.html)
supplies `partial_fit`; no hand-written optimizer or executable model pickle.

The `iex-sgd-v1` research lane now keeps separate AAPL/MSFT/QQQ/SPY models.
Every five-minute collection may persist one fresh forecast per symbol. Its
target is the close of the exact minute ten minutes after the source minute.
Three fixed-scale lagged returns use six contiguous same-session observations.
Missing/stale inputs and targets across the closing bell produce no forecast.
Each record freezes inputs, baseline probability, model probability and issue
time. Missing outcomes expire sixteen minutes after the target minute opens;
late backfills cannot rescue an expired prediction. Restart reconstructs the
model deterministically from at most the latest 1,000 scored records per symbol.
This is bounded replay, not an ever-growing in-memory agent or historical warmup.

The dashboard reports pending, expired and scored forecasts and rolling Brier
scores against an expanding-frequency baseline within that same training window.
These overlapping ten-minute price-direction predictions are not independent
trades, cost-adjusted returns, an untouched holdout, or proof of profitability.
They do not promote a model, authorize a broker, place orders, or change risk
gates. New algorithms/features require a new version rather than rewriting prior
predictions. Raw IEX observations retain the single-exchange limitations above.

Keep the existing PostgreSQL/Celery control plane, immutable model artifacts,
holdout reservations, independent risk worker, watchdog, broker reconciliation
and approval boundary. Use bounded tasks per company/strategy, not a permanently
busy process per chart. More workers help only until CPU, memory or API limits
become the bottleneck. Shared portfolio risk must consider correlations across
the specialist tasks.

The current local deliverable is a usable authenticated research console with
persistent collectors and worker monitoring. It is not a qualified autonomous
money manager. The prior challenger had worse Brier score and log loss than its
baseline; its report now says so rather than marking holdout completion a pass.
Research quality, accounting correctness, continuous recovery evidence and a
working private cloud host remain separate acceptance criteria.
