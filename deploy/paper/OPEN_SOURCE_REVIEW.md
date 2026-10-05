# Open-source integration review

Reviewed 2026-09-28. This is a targeted repository/documentation survey and an
implementation review of the two adopted adapters, not a security audit of every
project. Stars, README backtests, and agent debates are not evidence of profit.
No external agent was given our credentials and no repository's setup scripts
were executed. Adopted Python packages are version-pinned in requirements.txt.

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
