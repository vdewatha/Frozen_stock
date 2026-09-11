"""Offline, immutable research runs. These artifacts never authorize trading.

Input snapshots are caller-supplied research data; provenance is not certified.
Persisted joblib files are local artifacts and must never be loaded from uploads.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import tempfile
import warnings
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import RandomForestClassifier
from sklearn.exceptions import ConvergenceWarning
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from app.services.feature_pipeline import feature_config_id
from app.services.probabilistic_model import FEATURE_COLUMNS, HORIZONS, build_feature_frame


def _json(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, indent=2, allow_nan=False).encode()


def train_research_run(prices: pd.DataFrame, output: Path, *, symbol: str,
                       source: str, horizon: int = 5, seed: int = 42,
                       instrument_id: str | None = None, timeframe_minutes: int | None = None) -> dict:
    """Reserve the final 20% for evaluation; no tuning uses this holdout.

    Comparisons describe probability quality, not trading returns. Repeated
    inspection of the holdout invalidates its independence for model selection.
    """
    if horizon not in HORIZONS or not symbol.strip() or not source.strip():
        raise ValueError("Symbol, source and a supported horizon are required")
    if not {"date", "close", "volume"}.issubset(prices.columns):
        raise ValueError("Required CSV columns: date, close, volume")
    clean = prices[["date", "close", "volume"]].copy()
    dates = pd.to_datetime(clean.date, utc=True, errors="raise")
    if dates.isna().any() or dates.duplicated().any() or not dates.is_monotonic_increasing:
        raise ValueError("Dates must be non-null, unique and increasing")
    if instrument_id is not None or timeframe_minutes is not None:
        if instrument_id != "crypto_spot:KRAKEN:BTC:USD" or type(timeframe_minutes) is not int or timeframe_minutes != 60 or symbol.strip().upper() not in {"BTC/USD", "BTC-USD"}:
            raise ValueError("Portable inference currently supports Kraken BTC/USD hourly only")
        if not (dates.diff().dropna() == pd.Timedelta(hours=1)).all() or any(d.minute or d.second or d.microsecond or d.nanosecond for d in dates):
            raise ValueError("Portable hourly training requires aligned contiguous hourly candles")
    clean["date"] = dates.map(lambda value: value.isoformat())
    for name in ("close", "volume"):
        clean[name] = pd.to_numeric(clean[name], errors="raise").astype(float)
    if not np.isfinite(clean[["close", "volume"]]).all().all() or (clean.close <= 0).any() or (clean.volume < 0).any():
        raise ValueError("Prices must be positive, volume nonnegative, and all values finite")
    snapshot = clean.to_csv(index=False, float_format="%.17g").encode()
    frame = build_feature_frame(clean)
    target, end = f"target_up_{horizon}d", f"label_end_{horizon}d"
    eligible = frame.dropna(subset=FEATURE_COLUMNS + [target, end])
    boundary = int(len(eligible) * 0.8)
    test = eligible.iloc[boundary:]
    train = eligible.iloc[:boundary]
    if test.empty:
        raise ValueError("Insufficient holdout data")
    train = train.loc[train[end] < test.iloc[0].date]
    if len(train) < 100 or len(test) < 30 or train[target].nunique() < 2:
        raise ValueError("Need >=100 training rows, >=30 holdout rows and two training classes")
    models = {
        "logistic_regression": make_pipeline(StandardScaler(), LogisticRegression(random_state=seed, max_iter=1000, solver="liblinear")),
        "random_forest": RandomForestClassifier(n_estimators=120, min_samples_leaf=5, random_state=seed, n_jobs=1),
    }
    identity = {
        "format_version": 2, "symbol": symbol.strip().upper(), "source_claim": source,
        "instrument_id": instrument_id, "timeframe_minutes": timeframe_minutes,
        "horizon_bars": horizon, "seed": seed,
        "dataset_sha256": hashlib.sha256(snapshot).hexdigest(),
        "feature_config_id": feature_config_id(),
        "code_sha256": hashlib.sha256(b"".join(Path(__file__).with_name(name).read_bytes() for name in ("research_training.py", "feature_pipeline.py", "probabilistic_model.py"))).hexdigest(),
        "versions": {"python": platform.python_version(), "numpy": np.__version__, "pandas": pd.__version__, "sklearn": sklearn.__version__, "joblib": joblib.__version__,
                     "platform": platform.platform(), "machine": platform.machine()},
    }
    run_id = hashlib.sha256(_json(identity)).hexdigest()
    destination = Path(output) / run_id
    if destination.exists():
        raise FileExistsError("An immutable run with these inputs already exists")
    scores = {}
    predictions = pd.DataFrame({"date": test.date, "target": test[target]})
    for name, model in models.items():
        # Numerical warnings invalidate the experiment even when a library
        # returns finite-looking values. Never silently publish such artifacts.
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", RuntimeWarning)
                warnings.simplefilter("error", ConvergenceWarning)
                model.fit(train[FEATURE_COLUMNS], train[target])
                probability = model.predict_proba(test[FEATURE_COLUMNS])[:, 1]
        except (RuntimeWarning, ConvergenceWarning, FloatingPointError) as error:
            raise ValueError(f"Numerical validation failed for {name}; no run published") from error
        if not np.isfinite(probability).all() or ((probability < 0) | (probability > 1)).any():
            raise ValueError(f"Invalid probabilities for {name}; no run published")
        predictions[name] = probability
        scores[name] = {"brier_score": float(brier_score_loss(test[target], probability)),
                        "log_loss": float(log_loss(test[target], probability, labels=[0, 1]))}
    baseline = np.full(len(test), train[target].mean())
    scores["training_prevalence_baseline"] = {"brier_score": float(brier_score_loss(test[target], baseline)),
                                              "log_loss": float(log_loss(test[target], baseline, labels=[0, 1]))}
    manifest = identity | {"run_id": run_id, "status": "experimental", "eligible_for_trading": False,
        "train_rows": len(train), "holdout_rows": len(test), "train_end": str(train.iloc[-1].date),
        "train_label_end": str(train[end].max()), "holdout_start": str(test.iloc[0].date),
        "holdout_end": str(test.iloc[-1].date), "metrics": scores,
        "limitations": ["Caller-supplied data provenance is unverified", "No cost-aware trading qualification", "Legacy feature scaling retained", "No model promotion or live authorization"]}
    Path(output).mkdir(parents=True, exist_ok=True)
    # Same filesystem temporary directory makes publication atomic. Existing
    # nonempty destination directories cannot be replaced by rename.
    with tempfile.TemporaryDirectory(prefix=".training-", dir=output) as temporary:
        staging = Path(temporary)
        (staging / "dataset.csv").write_bytes(snapshot)
        predictions.to_csv(staging / "holdout_predictions.csv", index=False)
        for name, model in models.items():
            joblib.dump(model, staging / f"{name}.joblib")
        if instrument_id is not None:
            scaler, classifier = models["logistic_regression"].steps[0][1], models["logistic_regression"].steps[1][1]
            portable = {"version": 1, "run_id": run_id, "feature_config_id": feature_config_id(),
                        "feature_names": FEATURE_COLUMNS, "mean": scaler.mean_.tolist(), "scale": scaler.scale_.tolist(),
                        "coef": classifier.coef_[0].tolist(), "intercept": float(classifier.intercept_[0]),
                        "training_cutoff": manifest["train_label_end"], "instrument": instrument_id,
                        "timeframe_minutes": timeframe_minutes, "horizon_bars": horizon}
            (staging / "logistic_regression.json").write_bytes(_json(portable))
        manifest["files"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in staging.iterdir()}
        (staging / "manifest.json").write_bytes(_json(manifest))
        os.rename(staging, destination)
    return manifest
