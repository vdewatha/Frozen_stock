"""Separate cost-aware research candidates. No registration, promotion or orders.

Final test is untouched during model selection within a run. Repeated runs have
overlapping tests, so their results are not independent promotion evidence.
"""
import hashlib
import json
import os
from pathlib import Path
import platform
import tempfile
import warnings

import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import brier_score_loss, log_loss
from sklearn.exceptions import ConvergenceWarning
from app.services.feature_pipeline import DEFAULT_FEATURES, generate_features, feature_config_id

FEATURES = [s.name for s in DEFAULT_FEATURES]


def _json(value):
    return json.dumps(value, sort_keys=True, indent=2, allow_nan=False).encode()


def prepare_frame(prices, *, horizon, fee_rate, slippage_rate, gap_policy="strict"):
    if gap_policy not in ("strict", "segment"):
        raise ValueError("Unknown research gap policy")
    if type(horizon) is not int or horizon < 1 or horizon > 168:
        raise ValueError("horizon must be 1..168 hourly bars")
    if any(not np.isfinite(v) or not 0 <= v <= .05 for v in (fee_rate, slippage_rate)):
        raise ValueError("Per-side fees/slippage must be finite 0..5%")
    frame = prices[["date", "open", "close", "volume"]].copy().reset_index(drop=True)
    frame["date"] = pd.to_datetime(frame.date, utc=True, errors="raise")
    if (frame.date.isna().any() or frame.date.duplicated().any()
            or not frame.date.is_monotonic_increasing
            or (gap_policy == "strict" and not (frame.date.diff().dropna() == pd.Timedelta(hours=1)).all())
            or any(d.minute or d.second or d.microsecond or d.nanosecond for d in frame.date)):
        raise ValueError("Require contiguous aligned hourly observations")
    for col in ("open", "close", "volume"):
        frame[col] = pd.to_numeric(frame[col], errors="raise").astype(float)
    if not np.isfinite(frame[["open", "close", "volume"]]).all().all() or (frame[["open", "close"]] <= 0).any().any() or (frame.volume < 0).any():
        raise ValueError("Invalid observed prices or volume")
    if gap_policy == "segment":
        if frame.empty: raise ValueError("Empty segmented history")
        calendar_hours = int((frame.date.iloc[-1] - frame.date.iloc[0]) / pd.Timedelta(hours=1)) + 1
        if calendar_hours - len(frame) > 24:
            raise ValueError("Segment research permits at most 24 missing hourly observations")
        groups = frame.date.diff().ne(pd.Timedelta(hours=1)).cumsum()
        segments = [prepare_frame(segment, horizon=horizon, fee_rate=fee_rate,
                     slippage_rate=slippage_rate)[1] for _, segment in frame.groupby(groups)]
        return frame, pd.concat(segments, ignore_index=True)
    featured = generate_features(frame, version="2")
    entry, exit_price = frame.open.shift(-1), frame.open.shift(-(horizon + 1))
    with np.errstate(over="raise", invalid="raise", divide="raise"):
        net = exit_price * (1 - slippage_rate) * (1 - fee_rate) / (entry * (1 + slippage_rate) * (1 + fee_rate)) - 1
    if not np.isfinite(net.loc[entry.notna() & exit_price.notna()]).all():
        raise ValueError("Nonfinite cost-adjusted label")
    featured["net_return"] = net
    featured["target"] = (net > 0).astype(float).where(net.notna())
    featured["label_end"] = frame.date.shift(-(horizon + 1))
    return frame, featured.dropna(subset=FEATURES + ["target", "label_end"]).reset_index(drop=True)


def split_plan(frame, folds=3):
    if type(folds) is not int or not 2 <= folds <= 5:
        raise ValueError("Require 2..5 walk-forward folds")
    boundary = int(len(frame) * .8)
    final = frame.iloc[boundary:]
    if len(final) < 30:
        raise ValueError("Need at least 30 final test observations")
    development = frame.iloc[:boundary]
    development = development.loc[development.label_end < final.iloc[0].date]
    initial = len(development) // 2
    edges = np.linspace(initial, len(development), folds + 1, dtype=int)
    splits = []
    for left, right in zip(edges[:-1], edges[1:]):
        validation = development.iloc[left:right]
        if len(validation) < 20:
            raise ValueError("Need at least 20 observations per validation fold")
        train = development.iloc[:left]
        train = train.loc[train.label_end < validation.iloc[0].date]
        if len(train) < 100 or train.target.nunique() < 2:
            raise ValueError("Need 100 purged training observations and both net-return classes")
        splits.append((train, validation))
    return development, final, splits


def _model(name, seed):
    return (make_pipeline(StandardScaler(), LogisticRegression(solver="liblinear", max_iter=1000, random_state=seed))
            if name == "logistic_regression" else RandomForestClassifier(n_estimators=120, min_samples_leaf=5, random_state=seed, n_jobs=1))


def _fit_predict(model, train, validation):
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        warnings.simplefilter("error", ConvergenceWarning)
        model.fit(train[FEATURES], train.target)
        probability = model.predict_proba(validation[FEATURES])[:, 1]
    if not np.isfinite(probability).all() or ((probability < 0) | (probability > 1)).any():
        raise ValueError("Invalid model probabilities")
    return probability


def _metrics(target, probability):
    return {"brier_score": float(brier_score_loss(target, probability)),
            "log_loss": float(log_loss(target, probability, labels=[0, 1]))}


def train_research_v2(prices, output, *, source, symbol="BTC/USD", horizon=24,
                      fee_rate=.01, slippage_rate=.001, seed=42, folds=3):
    if symbol != "BTC/USD" or not isinstance(source, str) or not source.strip():
        raise ValueError("Explicit source and BTC/USD required")
    raw, frame = prepare_frame(prices, horizon=horizon, fee_rate=fee_rate, slippage_rate=slippage_rate)
    development, final, splits = split_plan(frame, folds)
    snapshot = raw.to_csv(index=False, float_format="%.17g").encode()
    identity = {"format": "cost-aware-research-v2", "symbol": symbol, "source_claim": source,
        "instrument": "crypto_spot:KRAKEN:BTC:USD", "timeframe_minutes": 60,
        "feature_config_id": feature_config_id(version="2"), "horizon_bars": horizon,
        "fee_rate_per_side": fee_rate, "slippage_rate_per_side": slippage_rate,
        "folds": folds, "seed": seed, "dataset_sha256": hashlib.sha256(snapshot).hexdigest(),
        "code_sha256": hashlib.sha256(Path(__file__).read_bytes() + Path(__file__).with_name("feature_pipeline.py").read_bytes()).hexdigest(),
        "versions": {"python": platform.python_version(), "numpy": np.__version__, "pandas": pd.__version__, "sklearn": sklearn.__version__}}
    run_id = hashlib.sha256(_json(identity)).hexdigest()
    destination = Path(output) / run_id
    if destination.exists():
        raise FileExistsError("Immutable candidate already exists")
    scores, validation_rows = {}, []
    for name in ("logistic_regression", "random_forest"):
        targets, probabilities = [], []
        for index, (train, validation) in enumerate(splits):
            prob = _fit_predict(_model(name, seed), train, validation)
            targets.extend(validation.target)
            probabilities.extend(prob)
            for date, target, probability in zip(validation.date, validation.target, prob):
                validation_rows.append({"model": name, "fold": index, "date": date, "target": target, "probability": probability})
        scores[name] = _metrics(targets, probabilities)
    selected = min(scores, key=lambda name: (scores[name]["brier_score"], name))
    model = _model(selected, seed)
    probability = _fit_predict(model, development, final)
    baseline = np.full(len(final), development.target.mean())
    manifest = identity | {"run_id": run_id, "status": "experimental", "eligible_for_trading": False,
        "selected_model": selected, "selection_metric": "pooled_walkforward_brier", "walkforward_metrics": scores,
        "final_test_metrics": _metrics(final.target, probability), "final_test_baseline": _metrics(final.target, baseline),
        "train_rows": len(development), "train_label_end": str(development.label_end.max()),
        "final_test_start": str(final.iloc[0].date), "final_test_end": str(final.iloc[-1].date), "final_test_rows": len(final),
        "split_audit": [{"train_label_end": str(t.label_end.max()), "validation_start": str(v.iloc[0].date), "validation_end": str(v.iloc[-1].date)} for t, v in splits],
        "limitations": ["Research only; no registry/paper binding or promotion", "Net labels assume next-open fills, no capacity or spread model",
            "Probability metrics are not realized strategy returns", "Repeated overlapping final tests are not independent selection evidence",
            "Caller source provenance is unverified; forward paper evidence required"]}
    Path(output).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".v2-training-", dir=output) as temporary:
        staging = Path(temporary)
        (staging / "dataset.csv").write_bytes(snapshot)
        pd.DataFrame(validation_rows).to_csv(staging / "walkforward_predictions.csv", index=False)
        pd.DataFrame({"date": final.date, "target": final.target, "net_return": final.net_return,
                      "probability": probability, "baseline": baseline}).to_csv(staging / "final_test_predictions.csv", index=False)
        # JSON logistic export is inert; RF remains research metrics only until
        # an independently reviewed portable runtime exists.
        if selected == "logistic_regression":
            scaler, classifier = model.steps[0][1], model.steps[1][1]
            portable = {"version": 2, "run_id": run_id, "feature_config_id": identity["feature_config_id"],
                "feature_names": FEATURES, "mean": scaler.mean_.tolist(), "scale": scaler.scale_.tolist(),
                "coef": classifier.coef_[0].tolist(), "intercept": float(classifier.intercept_[0]),
                "training_cutoff": manifest["train_label_end"], "eligible_for_trading": False}
            (staging / "logistic_regression.json").write_bytes(_json(portable))
        manifest["files"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in staging.iterdir()}
        (staging / "manifest.json").write_bytes(_json(manifest))
        os.rename(staging, destination)
    return manifest
