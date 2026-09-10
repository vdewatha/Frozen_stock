"""Calibrated immutable research candidates; no runtime or promotion authority."""
import hashlib
import io
import os
from pathlib import Path
import tempfile
import warnings
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.exceptions import ConvergenceWarning
from app.services.research_training_v2 import (prepare_frame, split_plan, _model,
    _fit_predict, _metrics, _json, FEATURES)
from app.services.feature_pipeline import feature_config_id


def partitions(frame):
    a, b = int(len(frame) * .6), int(len(frame) * .8)
    development, calibration, final = frame.iloc[:a], frame.iloc[a:b], frame.iloc[b:]
    if min(len(calibration), len(final)) < 50:
        raise ValueError("Require 50 calibration and final observations")
    development = development.loc[development.label_end < calibration.iloc[0].date]
    calibration = calibration.loc[calibration.label_end < final.iloc[0].date]
    if len(calibration) < 50 or calibration.target.nunique() != 2:
        raise ValueError("Require 50 purged calibration observations and both classes")
    return development, calibration, final


def _logit(probability):
    p = np.clip(probability, 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p))


def _sigmoid(values):
    return 1 / (1 + np.exp(-np.clip(values, -700, 700)))


def export_base(model, name):
    if name == "logistic_regression":
        scaler, classifier = model.steps[0][1], model.steps[1][1]
        return {"kind": name, "mean": scaler.mean_.tolist(), "scale": scaler.scale_.tolist(),
                "coef": classifier.coef_[0].tolist(), "intercept": float(classifier.intercept_[0])}
    trees = []
    for estimator in model.estimators_:
        tree = estimator.tree_
        if tree.node_count > 20000:
            raise ValueError("Forest exceeds portable tree bound")
        value = tree.value[:, 0, :]
        trees.append({"left": tree.children_left.tolist(), "right": tree.children_right.tolist(),
            "feature": tree.feature.tolist(), "threshold": tree.threshold.tolist(),
            "probability": (value[:, 1] / value.sum(axis=1)).tolist()})
    if len(trees) > 120:
        raise ValueError("Forest exceeds portable tree count")
    return {"kind": name, "trees": trees}


def predict_portable(spec, features):
    """Numeric research inference only; input must be this trainer's trusted export."""
    x = np.asarray(features, dtype=float)
    if x.ndim != 2 or x.shape[1] != len(FEATURES) or not np.isfinite(x).all():
        raise ValueError("Invalid research features")
    base = spec["base"]
    if base["kind"] == "logistic_regression":
        z = ((x - np.asarray(base["mean"])) / np.asarray(base["scale"])) * np.asarray(base["coef"])
        probability = _sigmoid(z.sum(axis=1) + base["intercept"])
    elif base["kind"] == "random_forest":
        if not 1 <= len(base["trees"]) <= 120:
            raise ValueError("Invalid tree count")
        x = x.astype(np.float32)  # sklearn forest prediction uses float32 input.
        votes = []
        for tree in base["trees"]:
            if not 1 <= len(tree["left"]) <= 20000:
                raise ValueError("Invalid tree size")
            nodes = np.zeros(len(x), dtype=int)
            for _ in range(len(tree["left"])):
                active = np.array([tree["left"][n] != -1 for n in nodes])
                if not active.any(): break
                for row in np.flatnonzero(active):
                    node = nodes[row]
                    nodes[row] = (tree["left"][node] if x[row, tree["feature"][node]] <= tree["threshold"][node] else tree["right"][node])
            else:
                raise ValueError("Cyclic tree")
            votes.append(np.array(tree["probability"])[nodes])
        probability = np.mean(votes, axis=0)
    else:
        raise ValueError("Unknown numeric model")
    calibrated = _sigmoid(_logit(probability) * spec["calibration"]["coef"] + spec["calibration"]["intercept"])
    if not np.isfinite(calibrated).all(): raise ValueError("Invalid calibrated prediction")
    return calibrated


def reliability(target, probability):
    target, probability = np.asarray(target), np.asarray(probability)
    result = []
    bins = np.minimum((probability * 10).astype(int), 9)
    for index in range(10):
        selected = bins == index
        result.append({"lower": index / 10, "upper": (index + 1) / 10, "count": int(selected.sum()),
            "mean_probability": float(probability[selected].mean()) if selected.any() else None,
            "observed_positive_rate": float(target[selected].mean()) if selected.any() else None})
    return result


def train_research_v3(prices, output, *, source, horizon=24, fee_rate=.01, slippage_rate=.001, seed=42, gap_policy="strict", fixed_model=None, source_snapshot=None):
    if not isinstance(source, str) or not source.strip(): raise ValueError("Source required")
    if fixed_model is not None and fixed_model != "random_forest":
        raise ValueError("Only random_forest is supported as a fixed challenger")
    raw, frame = prepare_frame(prices, horizon=horizon, fee_rate=fee_rate, slippage_rate=slippage_rate, gap_policy=gap_policy)
    if source_snapshot is not None:
        if not isinstance(source_snapshot, bytes) or not 0 < len(source_snapshot) <= 64 * 1024 * 1024:
            raise ValueError("Source snapshot must be bounded nonempty bytes")
        snapshot_raw, _ = prepare_frame(pd.read_csv(io.BytesIO(source_snapshot)), horizon=horizon,
            fee_rate=fee_rate, slippage_rate=slippage_rate, gap_policy=gap_policy)
        if not raw.equals(snapshot_raw):
            raise ValueError("Source snapshot differs from training observations")
    development, calibration, final = partitions(frame)
    # This inner split's holdout is unused; ALL selection folds remain inside
    # development. Neither calibration nor final test enters selection.
    scores = {}
    if fixed_model is None:
        _, _, folds = split_plan(development)
        for name in ("logistic_regression", "random_forest"):
            targets, probabilities = [], []
            for train, validation in folds:
                probabilities.extend(_fit_predict(_model(name, seed), train, validation))
                targets.extend(validation.target)
            scores[name] = _metrics(targets, probabilities)
        name = min(scores, key=lambda k: (scores[k]["brier_score"], k))
    else:
        # Explicit family choice: no score is used to choose a family or settings.
        name = fixed_model
    model = _model(name, seed)
    calibration_probability = _fit_predict(model, development, calibration)
    calibrator = LogisticRegression(solver="liblinear", random_state=seed, max_iter=1000)
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        warnings.simplefilter("error", ConvergenceWarning)
        calibrator.fit(_logit(calibration_probability).reshape(-1, 1), calibration.target)
    snapshot = source_snapshot if source_snapshot is not None else raw.to_csv(index=False, float_format="%.17g").encode()
    import sklearn, platform
    identity = {"format": "calibrated-research-v3", "dataset_sha256": hashlib.sha256(snapshot).hexdigest(),
        "source_claim": source, "instrument": "crypto_spot:KRAKEN:BTC:USD", "timeframe_minutes": 60,
        "feature_config_id": feature_config_id(version="2"), "horizon_bars": horizon,
        "fee_rate_per_side": fee_rate, "slippage_rate_per_side": slippage_rate, "seed": seed,
        "versions": {"numpy": np.__version__, "pandas": pd.__version__, "sklearn": sklearn.__version__, "python": platform.python_version()},
        "code_sha256": hashlib.sha256(b"".join(Path(__file__).with_name(n).read_bytes() for n in
            ("research_training_v3.py", "research_training_v2.py", "feature_pipeline.py"))).hexdigest()}
    if fixed_model is not None:
        identity["selection_policy"] = "fixed_random_forest_v1"
    if gap_policy == "segment":
        calendar = pd.date_range(raw.date.iloc[0], raw.date.iloc[-1], freq="h")
        missing = [value.isoformat() for value in calendar.difference(pd.DatetimeIndex(raw.date))]
        identity.update(gap_policy="segment", missing_hours=missing,
            missing_hours_sha256=hashlib.sha256(_json(missing)).hexdigest(), calendar_hours=len(calendar))
    run_id = hashlib.sha256(_json(identity)).hexdigest()
    spec = {"version": 3, "run_id": run_id, "feature_names": FEATURES, "feature_config_id": identity["feature_config_id"],
        "base": export_base(model, name), "calibration": {"method": "sigmoid_logit", "coef": float(calibrator.coef_[0, 0]),
            "intercept": float(calibrator.intercept_[0]), "probability_clip": [1e-6, 1 - 1e-6]},
        "training_cutoff": str(development.label_end.max()), "calibration_cutoff": str(calibration.label_end.max()),
        "mean_win": float(calibration.loc[calibration.net_return > 0, "net_return"].mean()),
        "mean_loss": float(-calibration.loc[calibration.net_return <= 0, "net_return"].mean()),
        "payoff_estimate_source": "calibration_only", "eligible_for_trading": False}
    probability = predict_portable(spec, final[FEATURES])
    raw_probability = predict_portable(spec | {"calibration": {"coef": 1., "intercept": 0.}}, final[FEATURES])
    manifest = identity | {"run_id": run_id, "status": "experimental", "eligible_for_trading": False,
        "selected_model": name, "walkforward_metrics": scores, "final_test_metrics": _metrics(final.target, probability),
        "clipped_uncalibrated_final_metrics": _metrics(final.target, raw_probability),
        "final_test_baseline": _metrics(final.target, np.full(len(final), development.target.mean())),
        "reliability_bins": reliability(final.target, probability), "train_rows": len(development),
        "calibration_rows": len(calibration), "final_test_rows": len(final),
        "train_label_end": spec["training_cutoff"], "calibration_start": str(calibration.iloc[0].date),
        "calibration_label_end": spec["calibration_cutoff"], "final_test_start": str(final.iloc[0].date),
        "final_test_end": str(final.iloc[-1].date),
        "limitations": ["Research only; no automatic promotion", "Overlapping repeated final tests are not independent evidence",
            "Calibration payoffs are modeled net returns, not actual executable profits", "Input provenance remains unverified"]}
    if fixed_model is not None:
        manifest["limitations"].append("Fixed RF challenger reuses previously examined history; final metrics are retrospective, not a new untouched test")
    destination = Path(output) / run_id
    if destination.exists(): raise FileExistsError("Immutable calibrated candidate exists")
    Path(output).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".v3-training-", dir=output) as temporary:
        stage = Path(temporary)
        (stage / "dataset.csv").write_bytes(snapshot)
        (stage / "calibrated_model.json").write_bytes(_json(spec))
        pd.DataFrame({"date": final.date, "target": final.target, "probability": probability}).to_csv(stage / "final_test_predictions.csv", index=False)
        manifest["files"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in stage.iterdir()}
        (stage / "manifest.json").write_bytes(_json(manifest))
        os.rename(stage, destination)
    return manifest
