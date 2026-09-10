# Verified no-trade gaps and successful ML activation

## Cause

The 2025 trade download reached the end of the requested year. Its nine missing
hourly candles were not missing downloaded trade IDs. Fresh public Trades API
requests from each missing hour returned the same first subsequent trade already
stored, with the immediately preceding consecutive ID before the gap:

| Missing UTC hours | Last trade ID before | First trade ID after |
| --- | --- | --- |
| January 25, 15:00–17:00 | 79154932 (14:37:50) | 79154933 (18:06:44) |
| August 28, 09:00 | 86477429 (08:07:58) | 86477430 (10:04:36) |
| November 1, 16:00–20:00 | 89232141 (15:02:38) | 89232142 (21:46:15) |

These are provider-supported no-trade intervals, not independently proven
exchange completeness. We do not assert the cause of every pause. Public API
documentation: https://docs.kraken.com/api-reference/market-data/get-recent-trades .
The nine fresh responses, hashes and boundary identities are preserved in
`.paper-training/history-ml/backfill/gap-audit.json`.

## Fix

The strict assumption that a year must contain 8,760 *traded* hours was blocking
an otherwise useful dataset. Strict defaults remain unchanged. An explicit
reviewed opt-in now permits at most 24 audited missing hours, with exact calendar
accounting and no invented prices, volumes or timestamps. This dataset contains
8,751 actual candles plus nine declared absent hours.

Raw-page integrity and aggregate reconstruction are still checked. Feature and
label generation restart in each contiguous segment, so warmup windows and
24-hour targets do not cross pauses. Source, audit and gap-policy hashes are
included in provenance/model identity and pinned for the new comparison.

## Verified deployment

On September 6, 2026 the workflow exported the validated dataset, trained both
cost profiles, and recorded its first on-time comparison observation at
16:03:34 UTC for the 16:00 UTC bar close. No model was substituted into the
original experiment. The stopped old container was preserved as
`trading-history-ml-workflow-pre-gap-fix`; the replacement runs under the original
managed name with image `trading-paper-comparison:20260906-gaps`.

Both profiles selected calibrated logistic regression. Each used 5,045 training,
1,665 calibration and 1,691 final-test rows after warmup/purging.

| Cost profile | Final Brier | Constant baseline Brier | First modeled probability | First decision |
| --- | --- | --- | --- | --- |
| Baseline | 0.129656 | 0.129442 | 15.56% | Hold |
| Stress | 0.088061 | 0.087846 | 6.48% | Hold |

Lower Brier is better: neither model beats its constant baseline. The two
profiles have different cost-dependent targets, so their absolute scores are
not an apples-to-apples model ranking. Probabilities concern modeled positive
net returns, not promises of executable profit. The first expected-net estimates
were negative. No entry threshold was relaxed to force a trade.

Independent reviews accepted both data auditing and segmented training. The
full backend suite passed 254 tests. No real funds, kill-switch changes, account
resets or automatic replacement of these pinned models were authorized.

To reproduce the explicit reviewed policy on a fresh deployment:

```sh
docker build -f backend/Dockerfile.paper -t trading-paper-comparison:20260906-gaps backend
python backend/scripts/start_history_ml_workflow.py --database .paper-venue/research.sqlite --output .paper-training/history-ml --allow-verified-gaps
```

The launcher refuses to duplicate an existing managed workflow. Inspect the
existing deployment instead of running this command again.
