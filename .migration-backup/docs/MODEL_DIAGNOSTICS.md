# Model diagnostics and walk-forward reports

Run from the project root with the backend Python dependencies installed:

```sh
python backend/scripts/report_model_diagnostics.py \
  --model /absolute/path/to/candidate/run_id \
  --manifest-sha256 VERIFIED_MANIFEST_SHA256 \
  --output /absolute/path/to/diagnostics --folds 3
```

Use the manifest pins in `.paper-training/history-ml/pinned_models.json`; translate its container `/research` prefix to the local history-ml directory. Output must be outside the candidate directory. No exchange connection or Docker service is required.

Each immutable, content-identified output directory contains `report.md`, the complete `report.json` (including every walk-forward forecast), and `files.json` with SHA-256 checksums. Repeating the same report identity fails rather than overwriting it. Identity incorporates input hashes, implementation hashes, dependency versions, seed, and fold count.

The report verifies the pinned artifact through the comparison loader, reconstructs the segmented dataset, and reproduces stored final predictions before reporting. Missing hours remain missing; features and labels do not cross data gaps.

Diagnostics include final-period Brier/log loss, reliability bins, calibration error, probability distribution, training-prevalence baseline, paired moving-block uncertainty, monthly/regime slices, feature drift, frozen entry-gate counts, cost sensitivity, and non-overlapping fixed-$100 label replays. JSON contains all detailed definitions. Replay amounts are hypothetical, not actual fills or portfolio performance.

Walk-forward evaluation uses only the original development period. Each expanding fold separates base fitting, subsequent calibration, and subsequent evaluation, purging labels that overlap the next partition. Logistic regression and random forest are evaluated alongside two past-prevalence baselines without selecting a winner. Two to five folds are supported; inadequate data or single-class training/calibration fails closed.

This historical data has already been examined. The report is exploratory, does not constitute a new untouched test, and does not promote models or change trading thresholds. Future improvements need a prospectively frozen future evaluation window. Lower probability loss does not establish profitable execution.

## September 10, 2026 results

Generated reports (private ignored research artifacts):

- Baseline: `.paper-training/history-ml/diagnostics/a0f3f81a47abb2fc5c610ac3dbccae17ac48a6c5a20944b2564a7d8aaebf0746/report.md`
- Stress: `.paper-training/history-ml/diagnostics/55eb02317e334728ba524abdc86123bdb828f5e2c1734d642f4e7a0de7fcf8b4/report.md`

Both pinned final-period models slightly underperform the constant training-prevalence forecast by Brier score; neither produces an entry passing the frozen gates. Random forest has the lowest pooled development walk-forward Brier score in both cost profiles (0.123927 baseline, 0.080693 stress), but this is exploratory evidence, not authorization to replace the pinned models.

Generation used the project's pinned NumPy 2.0.1, pandas 2.2.2 and scikit-learn 1.5.1 in an isolated local environment. Apple's Accelerate-backed NumPy wheel raised numerical RuntimeWarnings, so the macOS 11 OpenBLAS wheel of the same NumPy version was used with `OPENBLAS_NUM_THREADS=1`; warnings were not suppressed. Reports record the numerical libraries and runtime versions. On macOS, run tests with `TMPDIR=/private/tmp` to avoid the `/tmp` symlink conflicting with the artifact loader's symlink protection.
