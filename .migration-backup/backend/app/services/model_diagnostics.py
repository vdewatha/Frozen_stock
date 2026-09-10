"""Read-only diagnostics of pinned research artifacts; never model promotion."""
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import numpy as np
import pandas as pd
from app.services.comparison_model import load_comparison_model
from app.services.research_training_v2 import prepare_frame, FEATURES, _metrics, _json
from app.services.research_training_v3 import partitions, predict_portable, reliability


def summarize(target, probability):
    y, p = np.asarray(target, dtype=float), np.asarray(probability, dtype=float)
    if y.ndim != 1 or p.ndim != 1 or y.shape != p.shape:
        raise ValueError('Aligned one-dimensional predictions required')
    if not len(y):
        return {'rows': 0, 'metrics': None, 'reliability': []}
    if y.shape != p.shape or not np.isfinite(p).all() or not np.isin(y, [0, 1]).all() or ((p < 0) | (p > 1)).any():
        raise ValueError('Invalid diagnostic predictions')
    bins = reliability(y, p)
    return {'rows': len(y), 'positive_rate': float(y.mean()), 'mean_probability': float(p.mean()),
        'probability_quantiles': dict(zip(['min', 'p10', 'median', 'p90', 'max'], np.quantile(p, [0, .1, .5, .9, 1]).tolist())),
        'metrics': _metrics(y, p), 'reliability': bins,
        'expected_calibration_error_10_bins': sum(b['count'] * abs(b['mean_probability'] - b['observed_positive_rate']) for b in bins if b['count']) / len(y)}


def paired_block_interval(dates, target, probability, baseline, *, block_hours=48, repetitions=500, seed=42):
    """Descriptive paired moving-block bootstrap; blocks never cross time gaps."""
    if type(block_hours) is not int or block_hours < 1 or type(repetitions) is not int or not 100 <= repetitions <= 2000:
        raise ValueError('Invalid bounded bootstrap policy')
    dates = pd.DatetimeIndex(pd.to_datetime(dates, utc=True))
    y, p, base = map(lambda x: np.asarray(x, dtype=float), (target, probability, baseline))
    if not len(y) or any(len(v) != len(dates) for v in (y, p, base)) or dates.has_duplicates or not dates.is_monotonic_increasing:
        raise ValueError('Invalid paired observations')
    summarize(y, p); summarize(y, base)
    delta = (p-y)**2 - (base-y)**2
    groups = np.split(np.arange(len(y)), np.flatnonzero(np.diff(dates.asi8) != 3600 * 10**9) + 1)
    blocks = [delta[g[i:i+block_hours]] for g in groups for i in range(max(0, len(g)-block_hours+1))]
    result = {'mean_brier_difference': float(delta.mean()), 'negative_favors_model': True,
        'block_hours': block_hours, 'repetitions': repetitions, 'seed': seed,
        'method': 'paired_moving_blocks_no_gap_crossing', 'interpretation': 'descriptive_not_independent_profit_evidence'}
    if not blocks or any(len(g) < block_hours for g in groups) or len(y) < 4 * block_hours:
        return result | {'interval_95': None, 'reason': 'insufficient_contiguous_observations_for_all_segments'}
    rng = np.random.default_rng(seed)
    means = [float(np.concatenate([blocks[i] for i in rng.integers(0, len(blocks), math.ceil(len(y)/block_hours))])[:len(y)].mean()) for _ in range(repetitions)]
    return result | {'interval_95': np.quantile(means, [.025, .975]).tolist()}


def label_replay(frame, selected):
    """Fixed $100 stake, non-overlapping labeled next-open/horizon-exit replay."""
    selected = np.asarray(selected)
    if selected.ndim != 1 or len(selected) != len(frame) or selected.dtype != bool:
        raise ValueError('One boolean selection per observation required')
    last_exit, trades = None, []
    for row, choose in zip(frame.itertuples(), selected):
        if not choose or (last_exit is not None and row.date < last_exit):
            continue
        last_exit = row.label_end
        trades.append(float(row.net_return))
    return {'completed_hypothetical_roundtrips': len(trades), 'wins': sum(v > 0 for v in trades),
        'losses': sum(v < 0 for v in trades), 'net_fixed_stake_dollars': float(sum(trades) * 100),
        'mean_net_return': float(np.mean(trades)) if trades else None,
        'stake_dollars': 100, 'actual_paper_pnl': False,
        'limitations': 'Next-open label proxy, fixed horizon, no liquidity/queue/partial-fill model; not the live comparison exit policy'}


def diagnose_frame(raw, frame, manifest, spec):
    development, calibration, final = partitions(frame)
    if (len(final) != manifest['final_test_rows'] or str(final.iloc[0].date) != manifest['final_test_start']
            or str(development.label_end.max()) != manifest['train_label_end']):
        raise ValueError('Reconstructed partition differs from pinned candidate')
    probability = predict_portable(spec, final[FEATURES])
    uncalibrated = predict_portable(spec | {'calibration': {'coef': 1., 'intercept': 0.}}, final[FEATURES])
    base = np.full(len(final), development.target.mean())
    expected = probability * spec['mean_win'] - (1-probability) * spec['mean_loss']
    selected = (probability >= .6) & (expected > 0)
    drift = []
    for name in FEATURES:
        low, high = development[name].quantile([.01, .99])
        drift.append({'feature': name, 'development_p01': float(low), 'development_p99': float(high),
            'final_fraction_outside': float(((final[name] < low) | (final[name] > high)).mean())})
    flat_cutoff = float(development.ma_distance_20_50.abs().median())
    vol_cutoff = float(development.volatility_20d.quantile(.75))
    regimes = np.where(final.ma_distance_20_50.abs() <= flat_cutoff, 'sideways',
                       np.where(final.ma_distance_20_50 > 0, 'uptrend', 'downtrend'))
    regime_reports = {}
    for name, mask in [(name, regimes == name) for name in ('sideways', 'uptrend', 'downtrend')] + [
            ('high_volatility', final.volatility_20d.to_numpy() > vol_cutoff),
            ('normal_volatility', final.volatility_20d.to_numpy() <= vol_cutoff)]:
        regime_reports[name] = {'model': summarize(final.target.to_numpy()[mask], probability[mask]),
                               'constant_baseline': summarize(final.target.to_numpy()[mask], base[mask])}
    monthly = {}
    months = final.date.dt.strftime('%Y-%m')
    for month in sorted(months.unique()):
        mask = (months == month).to_numpy()
        monthly[month] = {'model': summarize(final.target.to_numpy()[mask], probability[mask]),
                          'constant_baseline': summarize(final.target.to_numpy()[mask], base[mask])}
    fee, slip = manifest['fee_rate_per_side'], manifest['slippage_rate_per_side']
    factor = (1-slip)*(1-fee)/((1+slip)*(1+fee))
    gross = (1+final.net_return.to_numpy())/factor
    costs = []
    for name, f, s in [('zero_cost_reference', 0., 0.), ('pinned_costs', fee, slip),
                       ('higher_costs', min(.05, fee*1.5), min(.05, slip*2))]:
        net = gross * (1-s)*(1-f)/((1+s)*(1+f)) - 1
        scenario = final.copy(); scenario['net_return'] = net
        costs.append({'scenario': name, 'fee_per_side': f, 'slippage_per_side': s,
            'positive_label_rate': float((net > 0).mean()), 'mean_label_net_return': float(net.mean()),
            'frozen_model_signal_replay': label_replay(scenario, selected)})
    candidates = {'frozen_ml_entry_gate': selected,
        'always_enter_fixed_horizon': np.ones(len(final), dtype=bool),
        'trend_20_50_positive': (final.ma_distance_20_50 > 0).to_numpy(),
        'mean_reversion_rsi_below_30': (final.rsi_14 < -.4).to_numpy(),
        'cash_no_entries': np.zeros(len(final), dtype=bool)}
    return {'coverage': {'raw_rows': len(raw), 'usable_labeled_rows': len(frame),
            'warmup_or_horizon_rows_excluded': len(raw)-len(frame), 'training_rows': len(development),
            'calibration_rows': len(calibration), 'final_rows': len(final),
            'final_start': str(final.iloc[0].date), 'final_end': str(final.iloc[-1].date)},
        'pinned_final': summarize(final.target, probability), 'clipped_uncalibrated_final': summarize(final.target, uncalibrated),
        'constant_training_prevalence': summarize(final.target, base),
        'brier_skill_vs_constant': float(1-_metrics(final.target, probability)['brier_score']/_metrics(final.target, base)['brier_score']) if _metrics(final.target, base)['brier_score'] else None,
        'paired_uncertainty': paired_block_interval(final.date, final.target, probability, base, block_hours=max(48, manifest['horizon_bars']+1)),
        'entry_gate': {'probability_threshold': .6, 'expected_net_must_exceed': 0,
            'probability_pass_rows': int((probability >= .6).sum()), 'all_gates_pass_rows': int(selected.sum()),
            'mean_expected_net': float(expected.mean()), 'payoffs_source': 'original_calibration_only'},
        'feature_drift': drift, 'regime_definitions': {'sideways_abs_ma_distance_max': flat_cutoff,
            'high_volatility_min': vol_cutoff, 'threshold_source': 'development_only', 'windows_are_hourly_bars': True},
        'regimes': regime_reports, 'months': monthly, 'cost_sensitivity': costs,
        'simple_policy_replays': {name: label_replay(final, signals) for name, signals in candidates.items()},
        'calibrator': spec['calibration'], 'final_probabilities': probability.tolist()}


def build_report(directory, *, manifest_sha256, folds=3):
    directory = Path(directory)
    manifest_path = directory / 'manifest.json'
    if manifest_path.is_symlink() or not manifest_path.is_file() or manifest_path.stat().st_size > 2*1024*1024:
        raise ValueError('Bounded regular manifest required')
    payload = manifest_path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != manifest_sha256:
        raise ValueError('Pinned manifest mismatch')
    declared = json.loads(payload)
    manifest, spec = load_comparison_model(directory, manifest_sha256=manifest_sha256,
        fee_rate=declared['fee_rate_per_side'], slippage_rate=declared['slippage_rate_per_side'], source_claim=declared['source_claim'],
        expected_horizon=declared['horizon_bars'])
    prices = pd.read_csv(directory / 'dataset.csv')
    raw, frame = prepare_frame(prices, horizon=manifest['horizon_bars'], fee_rate=manifest['fee_rate_per_side'],
        slippage_rate=manifest['slippage_rate_per_side'], gap_policy=manifest.get('gap_policy', 'strict'))
    diagnostics = diagnose_frame(raw, frame, manifest, spec)
    final = partitions(frame)[2]
    published = pd.read_csv(directory / 'final_test_predictions.csv')
    if (len(published) != len(final) or not np.array_equal(pd.to_datetime(published.date, utc=True), final.date)
            or not np.array_equal(published.target, final.target)
            or not np.allclose(published.probability, diagnostics['final_probabilities'], atol=1e-12, rtol=0)):
        raise ValueError('Pinned final predictions do not reproduce')
    from app.services.research_walkforward import evaluate_walkforward
    walkforward = evaluate_walkforward(frame, folds=folds, seed=42)
    import sklearn
    import scipy
    import platform
    from threadpoolctl import threadpool_info
    identity = {'format': 'model-diagnostics-v1', 'model_run_id': manifest['run_id'], 'manifest_sha256': manifest_sha256,
        'dataset_sha256': manifest['dataset_sha256'], 'folds': folds, 'seed': 42,
        'versions': {'numpy': np.__version__, 'pandas': pd.__version__, 'sklearn': sklearn.__version__,
                     'scipy': scipy.__version__, 'python': platform.python_version(), 'platform': platform.platform()},
        'numeric_libraries': [{key: lib.get(key) for key in ('internal_api', 'version', 'architecture', 'num_threads')}
                              for lib in threadpool_info()],
        'code_sha256': hashlib.sha256(b''.join(Path(__file__).with_name(name).read_bytes() for name in
            ('model_diagnostics.py', 'research_walkforward.py', 'research_training_v2.py', 'research_training_v3.py', 'feature_pipeline.py', 'comparison_model.py'))).hexdigest()}
    diagnostics['final_predictions'] = [
        {'date': row.date.isoformat(), 'label_end': row.label_end.isoformat(), 'target': int(row.target),
         'probability': float(p), 'net_return': float(row.net_return)}
        for row, p in zip(final.itertuples(), diagnostics.pop('final_probabilities'))]
    return identity | {'report_id': hashlib.sha256(_json(identity)).hexdigest(), 'source_claim': manifest['source_claim'],
        'selected_pinned_model': manifest['selected_model'], 'gap_policy': manifest.get('gap_policy', 'strict'),
        'missing_hours': manifest.get('missing_hours', []), 'diagnostics': diagnostics, 'walkforward': walkforward,
        'eligible_for_trading': False, 'models_changed': False,
        'limitations': ['Previously examined historical data, not a new untouched test or forward paper evidence',
            'Walk-forward fits use original development data only; original calibration/final periods excluded',
            'No winner selection, promotion, threshold change, broker access or runtime writes',
            'Overlapping targets are dependent; bootstrap intervals are descriptive and not multiple-testing corrected',
            'Counterfactual costs change the target; fixed probabilities are not recalibrated for those scenarios',
            'Regimes and feature drift are diagnostics, not automatic entry/OOD gates',
            'A new future evaluation window must be prospectively frozen before judging subsequent changes'],
        'references': ['https://scikit-learn.org/stable/modules/calibration.html',
                       'https://scikit-learn.org/stable/modules/cross_validation.html']}


def publish_report(report, output, markdown):
    report_id = report['report_id']
    if not isinstance(report_id, str) or len(report_id) != 64 or any(c not in '0123456789abcdef' for c in report_id):
        raise ValueError('Report ID must be a SHA-256 hex digest')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    destination = output / report['report_id']
    if destination.exists():
        raise FileExistsError('Immutable diagnostic report already exists')
    with tempfile.TemporaryDirectory(prefix='.diagnostics-', dir=output) as temp:
        stage = Path(temp)
        (stage / 'report.json').write_bytes(_json(report))
        (stage / 'report.md').write_text(markdown)
        files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in stage.iterdir()}
        (stage / 'files.json').write_bytes(_json(files))
        os.rename(stage, destination)
    return destination


def render_markdown(report):
    """Human-readable companion to the complete, machine-readable report."""
    d, w = report['diagnostics'], report['walkforward']
    def fmt(value):
        return 'n/a' if value is None else f'{value:.6f}' if isinstance(value, float) else str(value)
    lines = ['# Model diagnostics and walk-forward evaluation', '',
        f"Model: `{report['model_run_id']}`", '', f"Report: `{report['report_id']}`", '',
        '## Interpretation', '', *['- ' + item for item in report['limitations']], '',
        '## Pinned final-period probability quality', '',
        'Lower Brier score and log loss are better; neither measures trading profitability. '
        'Brier score reflects calibration and discrimination, not calibration alone.', '',
        '| Forecast | Rows | Positive rate | Mean probability | Brier | Log loss |',
        '|---|---:|---:|---:|---:|---:|']
    for name in ('pinned_final', 'clipped_uncalibrated_final', 'constant_training_prevalence'):
        s = d[name]
        lines.append('| ' + ' | '.join(map(fmt, [name, s['rows'], s['positive_rate'], s['mean_probability'], s['metrics']['brier_score'], s['metrics']['log_loss']])) + ' |')
    lines += ['', f"Brier skill versus constant: {fmt(d['brier_skill_vs_constant'])} (positive favors model).", '',
        f"Paired Brier difference: {fmt(d['paired_uncertainty']['mean_brier_difference'])}; descriptive 95% block interval: {d['paired_uncertainty']['interval_95']}. Negative favors model.", '',
        '## Coverage and frozen entry gate', '',
        *[f'- {k}: {v}' for k, v in d['coverage'].items()],
        f"- Gap policy: {report['gap_policy']}; missing hours: {len(report['missing_hours'])}",
        *[f'- {k}: {v}' for k, v in d['entry_gate'].items()], '',
        '## Reliability', '', '| Probability bin | Count | Mean forecast | Observed positive rate |', '|---|---:|---:|---:|']
    for b in d['pinned_final']['reliability']:
        lines.append(f"| {b['lower']:.1f}–{b['upper']:.1f} | {b['count']} | {fmt(b['mean_probability'])} | {fmt(b['observed_positive_rate'])} |")
    lines += ['', '## Development-only walk-forward evaluation', '',
        'Fixed model families are refitted on expanding past windows, with separate past calibration and purged label boundaries. '
        'Original calibration and final periods are excluded. These are already-seen historical observations.', '',
        '| Model | Pooled Brier | Pooled log loss |', '|---|---:|---:|']
    for name, m in w['pooled_metrics'].items():
        lines.append(f"| {name} | {fmt(m['brier_score'])} | {fmt(m['log_loss'])} |")
    lines += ['', '### Fold audit and scores', '']
    for audit in w['audit']:
        lines += [f"#### Fold {audit['fold'] + 1}", '', *[f'- {k}: {v}' for k, v in audit.items() if k != 'fold'], '',
            '| Model | Brier | Log loss |', '|---|---:|---:|']
        for name, values in w['fold_metrics'].items():
            m = values[audit['fold']]
            lines.append(f"| {name} | {fmt(m['brier_score'])} | {fmt(m['log_loss'])} |")
        lines.append('')
    for title, groups in [('Regimes', d['regimes']), ('Months', d['months'])]:
        lines += [f'## {title}', '', '| Group | Rows | Model Brier | Constant Brier |', '|---|---:|---:|---:|']
        for name, g in groups.items():
            model, base = g['model'], g['constant_baseline']
            lines.append(f"| {name} | {model['rows']} | {fmt((model['metrics'] or {}).get('brier_score'))} | {fmt((base['metrics'] or {}).get('brier_score'))} |")
        lines.append('')
    lines += ['## Feature drift', '', 'Fraction outside development 1st–99th percentile bounds; descriptive, not an entry filter.', '',
        '| Feature | Development p01 | Development p99 | Final outside fraction |', '|---|---:|---:|---:|']
    for row in d['feature_drift']:
        lines.append('| ' + ' | '.join(fmt(v) for v in row.values()) + ' |')
    lines += ['', '## Hypothetical fixed-stake policy replays', '',
        'Non-overlapping $100 next-open/fixed-horizon label proxies. These are not actual paper fills or portfolio returns; liquidity, queue position and partial fills are not modeled.', '',
        '| Policy | Round trips | Wins | Losses | Net dollars |', '|---|---:|---:|---:|---:|']
    for name, r in d['simple_policy_replays'].items():
        lines.append(f"| {name} | {r['completed_hypothetical_roundtrips']} | {r['wins']} | {r['losses']} | {fmt(r['net_fixed_stake_dollars'])} |")
    lines += ['', '## Cost sensitivity', '', 'Signals remain frozen under each counterfactual cost scenario.', '',
        '| Scenario | Fee/side | Slippage/side | Positive label rate | Mean label return |', '|---|---:|---:|---:|---:|']
    for r in d['cost_sensitivity']:
        lines.append('| ' + ' | '.join(fmt(r[k]) for k in ('scenario', 'fee_per_side', 'slippage_per_side', 'positive_label_rate', 'mean_label_net_return')) + ' |')
    lines += ['', '## Reproducibility', '', f"Manifest SHA-256: `{report['manifest_sha256']}`", '',
        f"Code SHA-256: `{report['code_sha256']}`", '', f"Library versions: {report['versions']}", '',
        'Complete predictions, reliability bins, regime definitions and audit metadata are in `report.json`; file hashes are in `files.json`.', '',
        *[f'- [Method reference]({url})' for url in report['references']], '']
    return '\n'.join(lines)
