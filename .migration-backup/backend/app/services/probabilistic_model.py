from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, brier_score_loss
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from app.services.feature_pipeline import DEFAULT_FEATURES, generate_features


FEATURE_COLUMNS = [spec.name for spec in DEFAULT_FEATURES]

HORIZONS = [1, 5, 20]


@dataclass
class TrainedModelResult:
    probability_up: float
    model_name: str


def build_feature_frame(prices: pd.DataFrame) -> pd.DataFrame:
    frame = generate_features(prices, DEFAULT_FEATURES)
    for horizon in HORIZONS:
        frame[f"future_return_{horizon}d"] = frame["close"].shift(-horizon) / frame["close"] - 1
        returns = frame[f"future_return_{horizon}d"]
        frame[f"target_up_{horizon}d"] = (returns > 0).astype(float).where(np.isfinite(returns))
        frame[f"label_end_{horizon}d"] = frame["date"].shift(-horizon)
    frame = frame.replace([np.inf, -np.inf], np.nan)
    frame[FEATURE_COLUMNS] = frame[FEATURE_COLUMNS].astype(float).clip(lower=-5, upper=5)
    return frame


def _fit_predict_probability(train_x: pd.DataFrame, train_y: pd.Series, latest_x: pd.DataFrame) -> list[TrainedModelResult]:
    results: list[TrainedModelResult] = []
    if train_y.nunique() < 2:
        return results

    logistic = make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000, random_state=42, solver="liblinear"))
    logistic.fit(train_x, train_y)
    results.append(TrainedModelResult(float(logistic.predict_proba(latest_x)[0][1]), "logistic_regression"))

    forest = RandomForestClassifier(n_estimators=120, min_samples_leaf=5, random_state=42)
    forest.fit(train_x, train_y)
    results.append(TrainedModelResult(float(forest.predict_proba(latest_x)[0][1]), "random_forest"))
    return results


def _walk_forward_for_horizon(model_frame: pd.DataFrame, horizon: int, train_window: int = 126, test_window: int = 21) -> list[dict]:
    target = f"target_up_{horizon}d"
    label_end = f"label_end_{horizon}d"
    if horizon not in HORIZONS or train_window < 1 or test_window < 1:
        raise ValueError("Invalid horizon or validation window")
    # End timestamps come from the original observations, before missing-feature
    # filtering. A positional gap alone is unsafe when observations are dropped.
    model_frame = model_frame.dropna(subset=FEATURE_COLUMNS + [target, label_end]).sort_values("date")
    folds: list[dict] = []
    fold_number = 1
    max_start = len(model_frame) - train_window - test_window
    for start in range(0, max_start + 1, test_window):
        train = model_frame.iloc[start : start + train_window]
        test = model_frame.iloc[start + train_window : start + train_window + test_window]
        train = train.loc[pd.to_datetime(train[label_end]) < pd.Timestamp(test.iloc[0]["date"])]
        if train[target].nunique() < 2 or test.empty:
            continue
        model = make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000, random_state=42, solver="liblinear"))
        model.fit(train[FEATURE_COLUMNS], train[target])
        probability = model.predict_proba(test[FEATURE_COLUMNS])[:, 1]
        prediction = (probability >= 0.5).astype(int)
        folds.append(
            {
                "horizon_days": horizon,
                "fold": fold_number,
                "train_start": str(train.iloc[0]["date"]),
                "train_end": str(train.iloc[-1]["date"]),
                "test_start": str(test.iloc[0]["date"]),
                "test_end": str(test.iloc[-1]["date"]),
                "sample_size": len(test),
                "train_sample_size": len(train),
                "purged_train_rows": train_window - len(train),
                "train_label_end": str(train[label_end].max()),
                "accuracy": round(float(accuracy_score(test[target], prediction)), 4),
                "brier_score": round(float(brier_score_loss(test[target], probability)), 4),
            }
        )
        fold_number += 1
    return folds


def predict_probabilities(symbol: str, prices: pd.DataFrame, source: str) -> dict:
    warnings: list[str] = []
    feature_frame = build_feature_frame(prices)
    model_frame = feature_frame.dropna(subset=FEATURE_COLUMNS).copy()
    latest_candidates = feature_frame.dropna(subset=FEATURE_COLUMNS)

    if len(model_frame) < 90 or latest_candidates.empty:
        return {
            "symbol": symbol,
            "source": source,
            "rows_used": len(prices),
            "latest_features": {},
            "predictions": [],
            "walk_forward": [],
            "warnings": ["Not enough clean market history for probabilistic modeling."],
        }

    latest_x = latest_candidates.iloc[[-1]][FEATURE_COLUMNS]
    latest_row = latest_candidates.iloc[-1]
    latest_features = {column: round(float(latest_x.iloc[0][column]), 6) for column in FEATURE_COLUMNS}
    predictions: list[dict] = []
    walk_forward: list[dict] = []

    for horizon in HORIZONS:
        target = f"target_up_{horizon}d"
        return_column = f"future_return_{horizon}d"
        training = model_frame.dropna(subset=[target, return_column]).copy()
        if len(training) < 90 or training[target].nunique() < 2:
            warnings.append(f"Horizon {horizon}d lacks enough positive and negative examples.")
            continue

        train_x = training[FEATURE_COLUMNS]
        train_y = training[target]
        model_results = _fit_predict_probability(train_x, train_y, latest_x)
        if not model_results:
            warnings.append(f"Horizon {horizon}d could not train a two-class classifier.")
            continue

        probability_up = float(np.mean([result.probability_up for result in model_results]))
        comparable = training.assign(distance=(training[FEATURE_COLUMNS] - latest_x.iloc[0]).abs().sum(axis=1)).nsmallest(40, "distance")
        expected_return = float(comparable[return_column].mean()) if not comparable.empty else float(training[return_column].mean())
        predictions.append(
            {
                "horizon_days": horizon,
                "probability_up": round(probability_up, 4),
                "probability_down": round(1 - probability_up, 4),
                "expected_return": round(expected_return, 4),
                "probabilities_by_model": {result.model_name: round(result.probability_up, 4) for result in model_results},
            }
        )
        walk_forward.extend(_walk_forward_for_horizon(training, horizon))

    if not walk_forward:
        warnings.append("Walk-forward validation did not produce folds; add more history before promotion decisions.")

    return {
        "symbol": symbol,
        "source": source,
        "rows_used": len(prices),
        "prediction_date": str(latest_row["date"]),
        "latest_close": round(float(latest_row["close"]), 6),
        "latest_features": latest_features,
        "predictions": predictions,
        "walk_forward": walk_forward,
        "warnings": warnings,
    }
