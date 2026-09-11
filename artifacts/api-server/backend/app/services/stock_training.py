"""Deterministic, offline training for verified daily-equity snapshots.

This is intentionally separate from the legacy crypto research trainers.  It
accepts only ``StockDataset`` instances built from persisted verified equity
observations, performs all family selection on purged expanding validation,
and touches the final holdout once for the selected calibrated model.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import platform
import tempfile
import warnings
from typing import Any, Callable, Iterable

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

from app.services.feature_pipeline import DEFAULT_FEATURES, feature_config_id, generate_features
from app.services.stock_dataset import (
    StockDataset,
    UnverifiedStockDataset,
    VERIFIED_EQUITY_PROVIDERS,
    _dataset_bytes,
    verify_stock_dataset_artifact,
)


FEATURES = [spec.name for spec in DEFAULT_FEATURES]
TRAINING_FORMAT = "verified-stock-training-v1"


class TrainingCancelled(RuntimeError):
    """A cooperative cancellation requested before immutable publication."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _check_cancelled(cancel_requested: Callable[[], bool] | None) -> None:
    if cancel_requested is not None and cancel_requested():
        raise TrainingCancelled("Stock training job cancellation was requested")


def _training_identity(
    dataset: StockDataset, *, horizon: int, fee_rate: float, slippage_rate: float,
    folds: int, embargo_days: int, seed: int,
) -> dict[str, Any]:
    """Return the pre-fit, content-addressed identity for a training request.

    Selection results and metrics are deliberately absent.  This lets a retry
    validate and adopt an already-published run before it can fit a candidate
    or evaluate the reserved final holdout again.
    """
    return {
        "format": TRAINING_FORMAT,
        "dataset_snapshot_id": dataset.snapshot_id,
        "dataset_sha256": dataset.dataset_sha256,
        "dataset_identity_sha256": dataset.identity_sha256,
        "dataset_artifact_sha256": dataset.metadata["artifact_sha256"],
        "provider": dataset.metadata["provider"],
        "universe_sha256": dataset.metadata["universe_sha256"],
        "cutoff_date": dataset.metadata["cutoff_date"],
        "feature_config_id": dataset.metadata["feature_config_id"],
        "horizon_days": horizon,
        "fee_rate_per_side": fee_rate,
        "slippage_rate_per_side": slippage_rate,
        "folds": folds,
        "embargo_days": embargo_days,
        "seed": seed,
        "candidate_model_families": ["logistic_regression", "random_forest"],
        "versions": {
            "python": platform.python_version(), "numpy": np.__version__, "pandas": pd.__version__,
            "sklearn": sklearn.__version__, "joblib": joblib.__version__,
        },
        "code_sha256": hashlib.sha256(
            Path(__file__).read_bytes() + Path(__file__).with_name("stock_dataset.py").read_bytes()
            + Path(__file__).with_name("feature_pipeline.py").read_bytes()
        ).hexdigest(),
    }


def _training_manifest_contract(identity: dict[str, Any], run_id: str) -> dict[str, Any]:
    """Static manifest values available before any fitting or holdout scoring."""
    return identity | {
        "run_id": run_id,
        "status": "experimental",
        "eligible_for_trading": False,
        "selection_policy": "minimum_pooled_purged_walkforward_brier_then_logloss_v1",
    }


def _holdout_evidence(
    evidence: dict[str, Any] | None, *, before_holdout: Callable[[], dict[str, Any]] | None,
) -> dict[str, Any]:
    """Return a validated, immutable record authorizing one holdout use."""
    if evidence is not None and before_holdout is not None:
        raise ValueError("Specify either standalone holdout evidence or a consumption callback, not both")
    if before_holdout is not None:
        evidence = before_holdout()
    elif evidence is None:
        # Direct/offline use has no durable job ledger. Production jobs always
        # supply the callback below, which commits a fenced consumption row
        # before this trainer is allowed to score the final holdout.
        evidence = {"count": 1, "claim": "standalone_trainer_use"}
    if not isinstance(evidence, dict) or type(evidence.get("count")) is not int or evidence["count"] != 1:
        raise ValueError("Final holdout consumption evidence must authorize exactly one use")
    claim = evidence.get("claim_sha256", evidence.get("claim"))
    if not isinstance(claim, str) or not claim:
        raise ValueError("Final holdout consumption evidence requires a claim")
    return dict(evidence)


def _model(name: str, seed: int):
    if name == "logistic_regression":
        return make_pipeline(StandardScaler(), LogisticRegression(solver="liblinear", random_state=seed, max_iter=1000))
    if name == "random_forest":
        return RandomForestClassifier(n_estimators=160, min_samples_leaf=5, random_state=seed, n_jobs=1)
    raise ValueError("Unsupported stock model family")


def _fit_probability(model, train: pd.DataFrame, test: pd.DataFrame) -> np.ndarray:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            warnings.simplefilter("error", ConvergenceWarning)
            model.fit(train[FEATURES], train.target.astype(int))
            probability = model.predict_proba(test[FEATURES])[:, 1]
    except (RuntimeWarning, ConvergenceWarning, FloatingPointError) as error:
        raise ValueError("Numerical failure during stock model fitting") from error
    if not np.isfinite(probability).all() or ((probability < 0) | (probability > 1)).any():
        raise ValueError("Stock model produced invalid probabilities")
    return np.asarray(probability, dtype=float)


def _metrics(target: Iterable[int], probability: Iterable[float]) -> dict[str, float]:
    y = np.asarray(list(target), dtype=int)
    p = np.asarray(list(probability), dtype=float)
    if len(y) == 0 or len(y) != len(p) or not np.isin(y, [0, 1]).all() or not np.isfinite(p).all():
        raise ValueError("Invalid probability metrics input")
    return {
        "brier_score": float(brier_score_loss(y, p)),
        "log_loss": float(log_loss(y, p, labels=[0, 1])),
        "sample_count": int(len(y)),
    }


def _strategy_metrics(rows: pd.DataFrame, probability: np.ndarray, *, threshold: float = 0.5) -> dict[str, Any]:
    """Cost-aware, one-position-at-a-time return accounting.

    Candidate labels already include entry/exit costs.  Selecting at most one
    candidate at a time and advancing through its ``label_end`` makes the
    reported return series non-overlapping even for a cross-sectional
    universe.  Losses remain in the sequence and therefore affect all
    aggregates and drawdown.
    """
    work = rows[["date", "label_end", "symbol", "net_return"]].copy()
    work["probability"] = probability
    work = work.loc[work.probability >= threshold].sort_values(
        ["date", "probability", "symbol"], ascending=[True, False, True], kind="mergesort"
    )
    selected: list[float] = []
    exits: list[str] = []
    available_after: pd.Timestamp | None = None
    for row in work.itertuples(index=False):
        if available_after is not None and row.date <= available_after:
            continue
        selected.append(float(row.net_return))
        available_after = row.label_end
        exits.append(pd.Timestamp(row.label_end).isoformat())
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for result in selected:
        equity *= 1.0 + result
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1.0)
    returns = np.asarray(selected, dtype=float)
    return {
        "sample_count": int(len(returns)),
        "wins": int((returns > 0).sum()),
        "losses": int((returns <= 0).sum()),
        "total_return": float(equity - 1.0),
        "mean_return": float(returns.mean()) if len(returns) else 0.0,
        "max_drawdown": float(max_drawdown),
        "trade_returns_include_losses": True,
        "non_overlapping": True,
        "last_label_end": exits[-1] if exits else None,
    }


def _verify_stock_training_dataset(
    dataset: StockDataset, *, fee_rate: float, slippage_rate: float,
) -> None:
    """Check immutable snapshot integrity without fitting or scoring a model."""
    if not isinstance(dataset, StockDataset):
        raise UnverifiedStockDataset("Training requires a verified StockDataset")
    # Do not train from the in-memory copy if its referenced immutable
    # snapshot has vanished.  This makes storage loss visible and prevents
    # silently treating a reconstructed DB query as the historical snapshot.
    snapshot_path = verify_stock_dataset_artifact(dataset)
    metadata = dataset.metadata
    if metadata.get("dataset_sha256") != dataset.dataset_sha256:
        raise UnverifiedStockDataset("Dataset metadata hash does not match its observations")
    if hashlib.sha256(_dataset_bytes(dataset.observations)).hexdigest() != dataset.dataset_sha256:
        raise UnverifiedStockDataset("Verified dataset observations were modified after snapshotting")
    if snapshot_path.read_bytes() != _dataset_bytes(dataset.observations):
        raise UnverifiedStockDataset("In-memory observations differ from immutable dataset artifact")
    if metadata.get("provider") not in VERIFIED_EQUITY_PROVIDERS:
        raise UnverifiedStockDataset("Dataset provider is not verified for stock training")
    expected_config = feature_config_id()
    if metadata.get("feature_config_id") != expected_config:
        raise UnverifiedStockDataset("Dataset feature contract is not supported by this trainer")
    if any(not np.isfinite(value) or not 0 <= value <= .05 for value in (fee_rate, slippage_rate)):
        raise ValueError("Fee and slippage must be finite rates in 0..5%")


def prepare_stock_frame(dataset: StockDataset, *, fee_rate: float, slippage_rate: float) -> pd.DataFrame:
    """Build causal features and next-open, cost-aware labels per stock."""
    _verify_stock_training_dataset(dataset, fee_rate=fee_rate, slippage_rate=slippage_rate)
    metadata = dataset.metadata
    frame = dataset.copy_observations()
    required = {"symbol", "date", "open", "close", "volume", "provider"}
    if not required.issubset(frame.columns) or not frame.provider.eq(metadata["provider"]).all():
        raise UnverifiedStockDataset("Dataset observations do not satisfy their provider binding")
    frame["date"] = pd.to_datetime(frame.date, utc=True, errors="raise")
    prepared: list[pd.DataFrame] = []
    for symbol, raw in frame.groupby("symbol", sort=True):
        raw = raw.sort_values("date", kind="mergesort").reset_index(drop=True).copy()
        if raw.date.duplicated().any() or not raw.date.is_monotonic_increasing:
            raise UnverifiedStockDataset(f"Invalid snapshot chronology for {symbol}")
        # The feature pipeline never uses a future price.  It intentionally
        # treats windows as observations rather than calendar days.
        featured = generate_features(raw[["date", "open", "close", "volume"]], version="1")
        entry = raw.open.shift(-1)
        exit_price = raw.open.shift(-(int(metadata["horizon_days"]) + 1))
        with np.errstate(over="raise", invalid="raise", divide="raise"):
            net = exit_price * (1 - slippage_rate) * (1 - fee_rate) / (
                entry * (1 + slippage_rate) * (1 + fee_rate)
            ) - 1
        featured["symbol"] = symbol
        featured["net_return"] = net
        featured["target"] = (net > 0).astype(float).where(net.notna())
        featured["label_end"] = raw.date.shift(-(int(metadata["horizon_days"]) + 1))
        prepared.append(featured)
    result = pd.concat(prepared, ignore_index=True)
    result = result.dropna(subset=FEATURES + ["target", "net_return", "label_end"]).copy()
    if result.empty or not np.isfinite(result[FEATURES + ["net_return"]].to_numpy(dtype=float)).all():
        raise ValueError("No finite stock feature/label observations are available")
    result["target"] = result.target.astype(int)
    return result.sort_values(["date", "symbol"], kind="mergesort").reset_index(drop=True)


@dataclass(frozen=True)
class StockSplit:
    train: pd.DataFrame
    validation: pd.DataFrame
    fold: int
    embargo_until: pd.Timestamp


def purged_expanding_walkforward(
    frame: pd.DataFrame,
    *,
    horizon_days: int,
    folds: int,
    embargo_days: int,
    minimum_train_rows: int = 100,
) -> tuple[pd.DataFrame, pd.DataFrame, list[StockSplit]]:
    """Return development/final-holdout and horizon-purged expanding folds."""
    if type(folds) is not int or not 2 <= folds <= 5:
        raise ValueError("folds must be in 2..5")
    if type(horizon_days) is not int or horizon_days < 1 or type(embargo_days) is not int or embargo_days < 0:
        raise ValueError("Invalid horizon or embargo")
    dates = pd.DatetimeIndex(sorted(pd.unique(frame.date)))
    boundary = int(len(dates) * 0.8)
    if boundary <= 0 or len(dates) - boundary < 20:
        raise ValueError("Need at least 20 distinct final holdout dates")
    final_start = dates[boundary]
    final = frame.loc[frame.date >= final_start].copy()
    # Any label touching final history is excluded from development.  The
    # additional embargo is explicit rather than assumed from label geometry.
    development = frame.loc[
        (frame.date < final_start) & (frame.label_end < final_start - timedelta(days=embargo_days))
    ].copy()
    dev_dates = pd.DatetimeIndex(sorted(pd.unique(development.date)))
    initial = len(dev_dates) // 2
    edges = np.linspace(initial, len(dev_dates), folds + 1, dtype=int)
    splits: list[StockSplit] = []
    for fold, (left, right) in enumerate(zip(edges[:-1], edges[1:])):
        validation_dates = dev_dates[left:right]
        if len(validation_dates) < 10:
            raise ValueError("Need at least 10 distinct dates per validation fold")
        validation_start = validation_dates[0]
        embargo_until = validation_start - timedelta(days=embargo_days)
        train = development.loc[
            (development.date < validation_start) & (development.label_end < embargo_until)
        ].copy()
        validation = development.loc[development.date.isin(validation_dates)].copy()
        if len(train) < minimum_train_rows or train.target.nunique() != 2:
            raise ValueError("Need sufficient two-class horizon-purged training observations")
        if validation.empty:
            raise ValueError("A walk-forward validation fold cannot be empty")
        if not (train.label_end < validation_start - timedelta(days=embargo_days)).all():
            raise AssertionError("Purge/embargo invariant violated")
        splits.append(StockSplit(train, validation, fold, embargo_until))
    if final.empty or development.empty:
        raise ValueError("Insufficient development or final holdout observations")
    return development, final, splits


def _calibration_split(development: pd.DataFrame, *, embargo_days: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    dates = pd.DatetimeIndex(sorted(pd.unique(development.date)))
    boundary = int(len(dates) * 0.8)
    if len(dates) - boundary < 10:
        raise ValueError("Need at least 10 calibration dates")
    calibration_start = dates[boundary]
    calibration = development.loc[development.date >= calibration_start].copy()
    train = development.loc[
        (development.date < calibration_start)
        & (development.label_end < calibration_start - timedelta(days=embargo_days))
    ].copy()
    if len(train) < 100 or train.target.nunique() != 2 or calibration.target.nunique() != 2:
        raise ValueError("Need two-class purged training and calibration samples")
    return train, calibration


def _fit_calibrator(probability: np.ndarray, target: pd.Series, *, seed: int) -> LogisticRegression:
    clipped = np.clip(probability, 1e-6, 1 - 1e-6)
    x = np.log(clipped / (1 - clipped)).reshape(-1, 1)
    calibrator = LogisticRegression(solver="liblinear", random_state=seed, max_iter=1000)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            warnings.simplefilter("error", ConvergenceWarning)
            calibrator.fit(x, target.astype(int))
    except (RuntimeWarning, ConvergenceWarning, FloatingPointError) as error:
        raise ValueError("Numerical failure during probability calibration") from error
    return calibrator


def _calibrated_probability(calibrator: LogisticRegression, probability: np.ndarray) -> np.ndarray:
    clipped = np.clip(probability, 1e-6, 1 - 1e-6)
    result = calibrator.predict_proba(np.log(clipped / (1 - clipped)).reshape(-1, 1))[:, 1]
    if not np.isfinite(result).all() or ((result < 0) | (result > 1)).any():
        raise ValueError("Calibrator produced invalid probabilities")
    return result


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate model manifest key")
        result[key] = value
    return result


def validate_stock_model_artifact(destination: Path, *, expected_manifest: dict[str, Any]) -> dict[str, Any]:
    """Validate a complete content-addressed artifact before retry adoption.

    A worker can die after the atomic directory rename and before the jobs
    service registers the model. Retrying must never overwrite that evidence;
    it may adopt it only when all identity, metadata, file-set, and digest
    checks prove it is exactly the artifact this deterministic request made.
    """
    destination = Path(destination)
    expected_files = {
        "dataset.csv", "selected_model.joblib", "calibrator.joblib",
        "walkforward_predictions.csv", "final_holdout_predictions.csv",
    }
    if destination.is_symlink() or not destination.is_dir():
        raise ValueError("Existing stock model artifact is not a regular directory")
    contents = {path.name: path for path in destination.iterdir()}
    if set(contents) != expected_files | {"manifest.json"}:
        raise ValueError("Existing stock model artifact is incomplete or has unexpected files")
    if any(path.is_symlink() or not path.is_file() for path in contents.values()):
        raise ValueError("Existing stock model artifact contains a nonregular file")
    try:
        manifest = json.loads(
            contents["manifest.json"].read_bytes(),
            object_pairs_hook=_unique_json_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite manifest number")),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("Existing stock model manifest is invalid") from error
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict):
        raise ValueError("Existing stock model manifest has no file digest inventory")
    # `expected_manifest` is a pre-fit contract.  It intentionally excludes
    # selected-family and metric values so a retry can adopt before it fits or
    # re-evaluates the holdout.  The published artifact must contain exactly
    # that contract plus the known result fields and its digest inventory.
    result_fields = {
        "selected_model", "walkforward_metrics", "calibration_metrics",
        "final_holdout_metrics", "final_holdout_baseline", "development_rows",
        "calibration_rows", "final_holdout_rows", "final_holdout_start",
        "final_holdout_end", "purged_expanding_walkforward", "limitations",
        "final_holdout_evaluation_count", "final_holdout_consumption",
    }
    if set(manifest) != set(expected_manifest) | result_fields | {"files"}:
        raise ValueError("Existing stock model manifest has an invalid field contract")
    if any(manifest.get(key) != value for key, value in expected_manifest.items()) or manifest.get("run_id") != destination.name:
        raise ValueError("Existing stock model manifest does not match this deterministic training request")
    if manifest.get("selected_model") not in expected_manifest["candidate_model_families"]:
        raise ValueError("Existing stock model manifest has an unsupported selected model")
    if not all(isinstance(manifest.get(key), dict) for key in (
        "walkforward_metrics", "calibration_metrics", "final_holdout_metrics", "final_holdout_baseline",
    )):
        raise ValueError("Existing stock model manifest has invalid result metrics")
    try:
        _holdout_evidence(manifest.get("final_holdout_consumption"), before_holdout=None)
    except ValueError as error:
        raise ValueError("Existing stock model manifest has invalid holdout consumption evidence") from error
    if manifest["final_holdout_evaluation_count"] != manifest["final_holdout_consumption"]["count"]:
        raise ValueError("Existing stock model manifest has inconsistent holdout use evidence")
    if set(manifest["files"]) != expected_files:
        raise ValueError("Existing stock model manifest file inventory is invalid")
    for name, digest in manifest["files"].items():
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError("Existing stock model manifest contains an invalid digest")
        if hashlib.sha256(contents[name].read_bytes()).hexdigest() != digest:
            raise ValueError("Existing stock model artifact file digest mismatch")
    if (
        manifest["files"]["dataset.csv"] != expected_manifest["dataset_sha256"]
        or hashlib.sha256(contents["dataset.csv"].read_bytes()).hexdigest() != expected_manifest["dataset_sha256"]
    ):
        raise ValueError("Existing stock model artifact references a different dataset")
    return manifest


def train_stock_model(
    dataset: StockDataset,
    output: Path,
    *,
    fee_rate: float = 0.0005,
    slippage_rate: float = 0.0005,
    folds: int = 3,
    embargo_days: int = 1,
    seed: int = 42,
    cancel_requested: Callable[[], bool] | None = None,
    before_holdout: Callable[[], dict[str, Any]] | None = None,
    holdout_consumption: dict[str, Any] | None = None,
    after_holdout_scored: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Train one immutable research artifact; never authorizes execution."""
    if type(seed) is not int:
        raise ValueError("seed must be an integer")
    _check_cancelled(cancel_requested)
    # Snapshot validation is allowed before adoption; model fitting, candidate
    # selection, calibration, and final-holdout scoring are not.
    _verify_stock_training_dataset(dataset, fee_rate=fee_rate, slippage_rate=slippage_rate)
    horizon = int(dataset.metadata["horizon_days"])
    if type(folds) is not int or not 2 <= folds <= 5:
        raise ValueError("folds must be in 2..5")
    if horizon < 1 or type(embargo_days) is not int or embargo_days < 0:
        raise ValueError("Invalid horizon or embargo")
    identity = _training_identity(
        dataset, horizon=horizon, fee_rate=fee_rate, slippage_rate=slippage_rate,
        folds=folds, embargo_days=embargo_days, seed=seed,
    )
    run_id = hashlib.sha256(_canonical(identity)).hexdigest()
    destination = Path(output) / run_id
    expected_manifest = _training_manifest_contract(identity, run_id)
    Path(output).mkdir(parents=True, exist_ok=True)
    _check_cancelled(cancel_requested)
    if destination.exists():
        return validate_stock_model_artifact(destination, expected_manifest=expected_manifest)

    frame = prepare_stock_frame(dataset, fee_rate=fee_rate, slippage_rate=slippage_rate)
    development, final, splits = purged_expanding_walkforward(
        frame, horizon_days=horizon, folds=folds, embargo_days=embargo_days
    )
    scores: dict[str, dict[str, Any]] = {}
    validation_predictions: list[dict[str, Any]] = []
    for name in ("logistic_regression", "random_forest"):
        _check_cancelled(cancel_requested)
        targets: list[int] = []
        probabilities: list[float] = []
        returns: list[pd.DataFrame] = []
        for split in splits:
            _check_cancelled(cancel_requested)
            probability = _fit_probability(_model(name, seed), split.train, split.validation)
            targets.extend(split.validation.target.tolist())
            probabilities.extend(probability.tolist())
            returns.append(split.validation)
            validation_predictions.extend(
                {
                    "model": name, "fold": split.fold, "date": row.date.isoformat(), "symbol": row.symbol,
                    "target": int(row.target), "net_return": float(row.net_return), "probability": float(prob),
                }
                for row, prob in zip(split.validation.itertuples(index=False), probability)
            )
        joined = pd.concat(returns, ignore_index=True)
        scores[name] = _metrics(targets, probabilities) | {
            "cost_aware_nonoverlapping_returns": _strategy_metrics(joined, np.asarray(probabilities)),
        }
    selected = min(scores, key=lambda name: (scores[name]["brier_score"], scores[name]["log_loss"], name))
    _check_cancelled(cancel_requested)

    calibration_train, calibration = _calibration_split(development, embargo_days=embargo_days)
    selected_model = _model(selected, seed)
    calibration_raw = _fit_probability(selected_model, calibration_train, calibration)
    calibrator = _fit_calibrator(calibration_raw, calibration.target, seed=seed)
    calibration_probability = _calibrated_probability(calibrator, calibration_raw)
    calibration_baseline = np.full(len(calibration), calibration_train.target.mean())
    calibration_metrics = {
        "selected_calibrated": _metrics(calibration.target, calibration_probability)
        | {"cost_aware_nonoverlapping_returns": _strategy_metrics(calibration, calibration_probability)},
        "training_prevalence_baseline": _metrics(calibration.target, calibration_baseline)
        | {"cost_aware_nonoverlapping_returns": _strategy_metrics(calibration, calibration_baseline)},
    }

    _check_cancelled(cancel_requested)
    # The final model is fitted only after selection is frozen.  Its holdout is
    # not read by any candidate family, calibrator, or threshold selection.
    # The jobs callback commits the fenced durable consumption transition here,
    # before any final-holdout fit or prediction can happen.
    consumption_evidence = _holdout_evidence(holdout_consumption, before_holdout=before_holdout)
    _check_cancelled(cancel_requested)
    final_model = _model(selected, seed)
    final_raw = _fit_probability(final_model, development, final)
    final_probability = _calibrated_probability(calibrator, final_raw)
    final_baseline = np.full(len(final), calibration_train.target.mean())
    final_metrics = _metrics(final.target, final_probability) | {
        "cost_aware_nonoverlapping_returns": _strategy_metrics(final, final_probability),
    }
    final_baseline_metrics = _metrics(final.target, final_baseline) | {
        "cost_aware_nonoverlapping_returns": _strategy_metrics(final, final_baseline),
    }
    if after_holdout_scored is not None:
        after_holdout_scored()

    audit = [
        {
            "fold": split.fold,
            "train_start": split.train.date.min().isoformat(), "train_end": split.train.date.max().isoformat(),
            "train_label_end": split.train.label_end.max().isoformat(),
            "validation_start": split.validation.date.min().isoformat(),
            "validation_end": split.validation.date.max().isoformat(),
            "embargo_until": split.embargo_until.isoformat(),
            "train_rows": int(len(split.train)), "validation_rows": int(len(split.validation)),
        }
        for split in splits
    ]
    manifest = expected_manifest | {
        "walkforward_metrics": scores, "selected_model": selected,
        "calibration_metrics": calibration_metrics,
        "final_holdout_metrics": final_metrics, "final_holdout_baseline": final_baseline_metrics,
        "final_holdout_evaluation_count": consumption_evidence["count"],
        "final_holdout_consumption": consumption_evidence,
        "development_rows": int(len(development)), "calibration_rows": int(len(calibration)),
        "final_holdout_rows": int(len(final)), "final_holdout_start": final.date.min().isoformat(),
        "final_holdout_end": final.date.max().isoformat(),
        "purged_expanding_walkforward": audit,
        "limitations": [
            "Offline research artifact only; it cannot place orders or authorize promotion.",
            "Final holdout was evaluated once after family selection and calibration.",
            "Returns use modeled next-open costs and one global non-overlapping position; they are not fills.",
            "Provider verification proves persisted importer provenance, not data-vendor completeness.",
        ],
    }
    _check_cancelled(cancel_requested)
    with tempfile.TemporaryDirectory(prefix=".stock-training-", dir=output) as temporary:
        stage = Path(temporary)
        (stage / "dataset.csv").write_bytes(_dataset_bytes(dataset.observations))
        joblib.dump(final_model, stage / "selected_model.joblib")
        joblib.dump(calibrator, stage / "calibrator.joblib")
        pd.DataFrame(validation_predictions).to_csv(stage / "walkforward_predictions.csv", index=False)
        pd.DataFrame(
            {
                "date": final.date, "symbol": final.symbol, "target": final.target,
                "net_return": final.net_return, "probability": final_probability, "baseline": final_baseline,
            }
        ).to_csv(stage / "final_holdout_predictions.csv", index=False)
        manifest["files"] = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(stage.iterdir())}
        (stage / "manifest.json").write_bytes(_canonical(manifest))
        _check_cancelled(cancel_requested)
        try:
            os.rename(stage, destination)
        except FileExistsError:
            _check_cancelled(cancel_requested)
            return validate_stock_model_artifact(destination, expected_manifest=expected_manifest)
    return manifest