"""Exploratory development-only diagnostics; no new untouched-test claim."""
import warnings
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.exceptions import ConvergenceWarning
from app.services.research_training_v2 import FEATURES, _model, _fit_predict, _metrics
from app.services.research_training_v3 import _logit, _sigmoid, reliability


def evaluate_walkforward(frame, folds=3, seed=42):
    """Accept full prepared v3 frame; internally exclude original calibration/test.

    Fold predictions describe already-seen historical development observations.
    Neither this evaluation nor its metrics may select/promote a live model.
    """
    if type(folds) is not int or not 2 <= folds <= 5:
        raise ValueError("Require 2..5 fixed walk-forward folds")
    required = FEATURES + ["date", "label_end", "target", "net_return"]
    if not set(required).issubset(frame.columns) or frame.empty:
        raise ValueError("Full prepared frame required")
    data = frame[required].copy().reset_index(drop=True)
    for field in ("date", "label_end"):
        data[field] = pd.to_datetime(data[field], utc=True, errors="raise")
    if (data.date.isna().any() or data.label_end.isna().any() or data.date.duplicated().any()
            or not data.date.is_monotonic_increasing or (data.label_end <= data.date).any()
            or not data.target.isin([0, 1]).all()
            or not np.isfinite(data[FEATURES + ["target", "net_return"]]).all().all()):
        raise ValueError("Invalid prepared research frame")
    # Same development boundary/purge as v3.partitions(frame)[0], without
    # requiring classes in the excluded original calibration partition.
    # Diagnostics must not depend on those later labels even for readiness.
    original_boundary = int(len(data) * .6)
    development = data.iloc[:original_boundary]
    development = development.loc[development.label_end < data.iloc[original_boundary].date]
    edges = np.linspace(len(development) // 2, len(development), folds + 1, dtype=int)
    forecasts, audits = [], []
    model_names = ("logistic_regression", "random_forest", "training_prevalence", "calibration_prevalence")
    fold_metrics = {name: [] for name in model_names}
    for fold, (left, right) in enumerate(zip(edges[:-1], edges[1:])):
        evaluation = development.iloc[left:right]
        past = development.iloc[:left]
        if len(evaluation) < 20: raise ValueError("Insufficient fold evaluation observations")
        past = past.loc[past.label_end < evaluation.iloc[0].date]
        boundary = int(len(past) * .8)
        training, calibration = past.iloc[:boundary], past.iloc[boundary:]
        if len(calibration) < 30: raise ValueError("Insufficient held-out calibration observations")
        training = training.loc[training.label_end < calibration.iloc[0].date]
        if len(training) < 100 or training.target.nunique() != 2 or calibration.target.nunique() != 2:
            raise ValueError("Need 100 purged base training rows and both classes in training/calibration")
        predictions = {
            "training_prevalence": np.full(len(evaluation), training.target.mean()),
            "calibration_prevalence": np.full(len(evaluation), calibration.target.mean())}
        for name in model_names[:2]:
            model = _model(name, seed)
            calibration_probability = _fit_predict(model, training, calibration)
            calibrator = LogisticRegression(solver="liblinear", max_iter=1000, random_state=seed)
            with warnings.catch_warnings():
                warnings.simplefilter("error", RuntimeWarning)
                warnings.simplefilter("error", ConvergenceWarning)
                calibrator.fit(_logit(calibration_probability).reshape(-1, 1), calibration.target)
                raw = model.predict_proba(evaluation[FEATURES])[:, 1]
                if not np.isfinite(raw).all() or ((raw < 0) | (raw > 1)).any():
                    raise ValueError("Invalid base fold probabilities")
                probability = _sigmoid(_logit(raw) * calibrator.coef_[0, 0] + calibrator.intercept_[0])
            if not np.isfinite(probability).all() or ((probability < 0) | (probability > 1)).any():
                raise ValueError("Invalid calibrated fold probabilities")
            predictions[name] = probability
        audits.append({"fold": fold, "training_start": training.iloc[0].date.isoformat(),
            "training_label_end": training.label_end.max().isoformat(),
            "calibration_start": calibration.iloc[0].date.isoformat(),
            "calibration_label_end": calibration.label_end.max().isoformat(),
            "evaluation_start": evaluation.iloc[0].date.isoformat(), "evaluation_end": evaluation.iloc[-1].date.isoformat(),
            "training_rows": len(training), "calibration_rows": len(calibration), "evaluation_rows": len(evaluation)})
        for name, probability in predictions.items():
            fold_metrics[name].append({"fold": fold, **_metrics(evaluation.target, probability),
                                      "reliability_bins": reliability(evaluation.target, probability)})
        for offset, row in enumerate(evaluation.itertuples()):
            forecasts.append({"date": row.date.isoformat(), "target": int(row.target), "net_return": float(row.net_return),
                "fold": fold, "models": {name: float(predictions[name][offset]) for name in model_names}})
    target = [row["target"] for row in forecasts]
    pooled = {}
    for name in model_names:
        probability = [row["models"][name] for row in forecasts]
        pooled[name] = _metrics(target, probability) | {"reliability_bins": reliability(target, probability)}
    return {"version": "development-walkforward-v1", "predictions": forecasts, "audit": audits,
        "pooled_metrics": pooled, "fold_metrics": fold_metrics,
        "original_development_start": development.iloc[0].date.isoformat(),
        "original_development_end": development.iloc[-1].date.isoformat(),
        "original_calibration_and_final_excluded": True, "eligible_for_trading": False,
        "new_untouched_test": False, "exploratory_already_seen_history": True,
        "limitations": ["Historical development data was already used in prior research",
            "Reserve future observations for genuinely new evaluation", "Probability metrics are not executable trading returns",
            "No model selection, registration, or promotion is performed"]}
