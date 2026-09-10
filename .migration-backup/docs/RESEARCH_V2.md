# Cost-aware research v2

`backend/scripts/train_research_v2.py` requires a CSV with `date`, `open`, `close`,
and `volume`, at least 8,760 contiguous aligned hourly observations, an explicit
source claim, and a new output location. It never registers a model, changes a
paper binding, submits orders, or grants eligibility. Legacy training is unchanged.

Alternatively use `--history-bundle PATH` from `import_kraken_history.py`; the
CLI verifies its hashes and incorporates its snapshot identity into the training
source. This avoids treating an arbitrary CSV filename as verified provenance.
The bundle still records provenance as a source claim, not vendor authentication.

The existing hourly trainer and active paper model continue on their original
version until a separately evaluated v2 candidate is ready. This increment does
not automatically create parallel broker portfolios or promote a model. Those
execution changes must preserve version-bound forward evidence and require a
separate integration step; the legacy v1 runtime intentionally rejects v2 models.

Defaults: BTC/USD, 24-hour horizon, **1% fee per side**, and 0.1% adverse slippage
per side. Fees have deliberately not been reduced to make results look better.
Override costs only with separately justified assumptions. Source provenance
remains unverified by this offline function.

Features use version 2: centered RSI with correct flat/up/down handling,
price-normalized MACD, and no legacy blanket clipping. Features at bar `t` use no
future observations. Labels enter at `open[t+1]` and exit at
`open[t+1+horizon]`, with both-side fees and adverse slippage applied. The target
is positive modeled net return, not merely an upward price move. These labels do
not model volume capacity, bid/ask spread, or actual fills.

The last 20% of label-complete observations is held out before selection. The
earlier segment is purged so its label endpoints precede the final-test start.
Three expanding chronological development folds each purge labels crossing the
validation boundary. Logistic regression and random forest are compared using
pooled validation Brier score. Only the chosen model is refit on the purged
development segment and evaluated on the final test, alongside a constant
training-prevalence baseline. Scaling is fitted separately within each training
fold, never on validation or final-test observations.

Immutable, atomically published artifacts include input snapshot, walk-forward
predictions, final-test predictions, split audit, cost/feature/code identities,
and artifact hashes. A selected logistic model also has an inert version-2 JSON
export. A selected forest currently has research metrics only; no unreviewed
forest runtime or executable serialization is introduced. Any numerical warning
blocks publication. Linux training is recommended because the existing macOS
Accelerate runtime has produced spurious matrix-multiplication warnings.

Probability scores are **not realized trading profits**. A final test is untouched
only within that run; repeated rolling runs have overlapping tests and cannot
turn repeated inspection into independent evidence. Forward, version-bound paper
evaluation and explicit promotion controls remain required.

## Verification and data availability

September 5, 2026: 224 tests passed in an isolated Linux container. Independent
review found a caller-index/label alignment bug, fixed by resetting the input
index before label construction; a regression proves index invariance. Computed
net labels also reject overflow/nonfinite results. Legacy services remain running
without a model switch.

The official Kraken archive was located via its OHLCVT support page. The archive
is 7,885,068,519 bytes. A bounded selective downloader was implemented and tested,
but actual partial requests returned inconsistent ranges; it failed closed and
no historical CSV was obtained. Therefore no longer-history v2 training run is
claimed. Historical input acquisition is still required. The importer supports
an explicit contiguous UTC date window (for example 2023 through 2025), records
the selection in the snapshot identity, and never fabricates gaps.
