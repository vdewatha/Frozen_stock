"""Meta-labeling filter for primary-strategy BUY signals (AFML ch. 3).

The primary strategies decide *direction* (long).  A secondary classifier
estimates the probability that the candidate trade, labelled with the
triple-barrier method in ``triple_barrier``, ends with a positive return after
costs.  Probabilities are calibrated on a held-out fold, and a threshold is
chosen on a later, separate validation fold to maximise net expectancy subject
to a minimum win rate (default 80%) and a minimum trade count.

Pipeline (offline, ``scripts/train_meta_label.py``):

1. ``build_events``: replay each strategy causally over daily history, label
   every BUY with ``label_events`` and join decision-time features.
2. ``walk_forward``: yearly test folds.  For test year Y the model trains on
   events whose labels end before year Y-2 (purged + embargoed), the
   calibrator is fitted on year Y-2, the threshold is selected on year Y-1,
   and year Y is scored once.  No fold ever sees a label that ends inside a
   later partition.
3. ``fit_final_model``: the same three-way split anchored at the end of the
   data; the result is persisted with ``save_model``.

Runtime (``score_buy_signal``): the paper cycle computes the same features
from the trusted daily frame (and SPY's) and records the calibrated
probability and the take/skip decision.  It never places or blocks orders
unless ``paper_meta_label_mode == "enforce"`` (off by default).

All features use trailing windows of at most 252 bars, so the 260-bar frame
the paper path loads is sufficient and the training and runtime feature code
are the same function.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd

from app.services.meta_label_primary import REPLAYABLE_STRATEGIES, replay_signals
from app.services.strategies.base import rsi as compute_rsi
from app.services.triple_barrier import BarrierConfig, average_uniqueness, label_events
from app.services.trading_universe import ETF_SYMBOLS, SECTOR_NAMES, sector_group

logger = logging.getLogger(__name__)

MODEL_FORMAT = "meta-label-v1"
SECTOR_KEYS = tuple(sorted(SECTOR_NAMES))
MARKET_FEATURES = (
    "ret_1", "ret_5", "ret_20", "ret_60", "vol_20", "vol_60", "atr_pct_14", "dist_sma_20", "dist_sma_50",
    "dist_sma_200", "sma_50_over_200", "rsi_14", "rsi_2", "ibs", "gap", "volume_ratio_20", "dist_high_252",
    "spy_above_sma_200", "spy_dist_sma_200", "spy_vol_20", "spy_vol_ratio", "spy_ret_5", "spy_ret_20",
    "buy_consensus",
)
MAX_FEATURE_LOOKBACK = 252


def feature_names(strategies: Iterable[str] = REPLAYABLE_STRATEGIES) -> list[str]:
    return [
        *MARKET_FEATURES,
        "primary_confidence", "primary_probability_up",
        *(f"dow_{day}" for day in range(5)),
        "is_etf",
        *(f"sector_{key}" for key in SECTOR_KEYS),
        *(f"strategy_{slug}" for slug in strategies),
    ]


# ---------------------------------------------------------------- features

def _normalize_dates(values) -> pd.Series:
    dates = pd.to_datetime(pd.Series(values).astype(str), utc=True, errors="coerce")
    return dates.dt.tz_convert(None).dt.normalize()


def _numeric(frame: pd.DataFrame, column: str) -> pd.Series:
    return pd.to_numeric(frame[column], errors="coerce").astype(float).reset_index(drop=True)


def _spy_features(spy: pd.DataFrame) -> pd.DataFrame:
    close = _numeric(spy, "close")
    log_returns = np.log(close).diff()
    sma_200 = close.rolling(200).mean()
    vol_20 = log_returns.rolling(20).std()
    vol_250 = log_returns.rolling(250).std()
    return pd.DataFrame({
        "date": _normalize_dates(spy["date"]).to_numpy(),
        "spy_above_sma_200": (close > sma_200).astype(float).where(sma_200.notna()),
        "spy_dist_sma_200": close / sma_200 - 1,
        "spy_vol_20": vol_20,
        "spy_vol_ratio": vol_20 / vol_250,
        "spy_ret_5": close.pct_change(5),
        "spy_ret_20": close.pct_change(20),
    })


def market_features(prices: pd.DataFrame, spy_prices: pd.DataFrame | None) -> pd.DataFrame:
    """Per-bar decision-time features for one symbol (row t uses bars <= t only)."""
    return _market_and_signals(prices, spy_prices)[0]


def _market_and_signals(prices: pd.DataFrame, spy_prices: pd.DataFrame | None) -> tuple[pd.DataFrame, dict]:
    frame = prices.reset_index(drop=True)
    close, open_ = _numeric(frame, "close"), _numeric(frame, "open")
    high = _numeric(frame, "high") if "high" in frame else pd.concat([open_, close], axis=1).max(axis=1)
    low = _numeric(frame, "low") if "low" in frame else pd.concat([open_, close], axis=1).min(axis=1)
    volume = _numeric(frame, "volume") if "volume" in frame else pd.Series(np.nan, index=frame.index)
    log_returns = np.log(close).diff()
    previous = close.shift(1)
    true_range = pd.concat([high - low, (high - previous).abs(), (low - previous).abs()], axis=1).max(axis=1)
    sma = {window: close.rolling(window).mean() for window in (20, 50, 200)}
    span = (high - low).replace(0, np.nan)
    out = pd.DataFrame({
        "ret_1": close.pct_change(1),
        "ret_5": close.pct_change(5),
        "ret_20": close.pct_change(20),
        "ret_60": close.pct_change(60),
        "vol_20": log_returns.rolling(20).std(),
        "vol_60": log_returns.rolling(60).std(),
        "atr_pct_14": true_range.rolling(14).mean() / close,
        "dist_sma_20": close / sma[20] - 1,
        "dist_sma_50": close / sma[50] - 1,
        "dist_sma_200": close / sma[200] - 1,
        "sma_50_over_200": sma[50] / sma[200] - 1,
        "rsi_14": compute_rsi(close, 14),
        "rsi_2": compute_rsi(close, 2),
        "ibs": ((close - low) / span).fillna(0.5),
        "gap": open_ / previous - 1,
        "volume_ratio_20": volume / volume.rolling(20).mean(),
        "dist_high_252": close / close.rolling(MAX_FEATURE_LOOKBACK).max() - 1,
    })
    dates = _normalize_dates(frame["date"])
    out["date"] = dates.to_numpy()
    out["dow"] = dates.dt.dayofweek.to_numpy()
    spy_columns = ["spy_above_sma_200", "spy_dist_sma_200", "spy_vol_20", "spy_vol_ratio", "spy_ret_5", "spy_ret_20"]
    if spy_prices is not None and len(spy_prices):
        spy = _spy_features(spy_prices).dropna(subset=["date"]).sort_values("date")
        order = out[["date"]].reset_index().dropna(subset=["date"]).sort_values("date")
        merged = pd.merge_asof(order, spy, on="date", direction="backward").set_index("index")
        for column in spy_columns:
            out[column] = merged[column].reindex(out.index)
    else:
        for column in spy_columns:
            out[column] = np.nan
    signals = replay_signals(frame)
    out["buy_consensus"] = sum((signals[slug]["action"] == "BUY").astype(float) for slug in REPLAYABLE_STRATEGIES)
    return out, signals


def _design_rows(market: pd.DataFrame, symbol: str, strategy: str, confidence, probability_up,
                 strategies: Iterable[str]) -> pd.DataFrame:
    rows = market[list(MARKET_FEATURES)].copy()
    rows["primary_confidence"] = np.asarray(confidence, dtype=float)
    rows["primary_probability_up"] = np.asarray(probability_up, dtype=float)
    dow = market["dow"].to_numpy()
    for day in range(5):
        rows[f"dow_{day}"] = (dow == day).astype(float)
    symbol = symbol.upper()
    rows["is_etf"] = float(symbol in ETF_SYMBOLS)
    group = sector_group(symbol)
    for key in SECTOR_KEYS:
        rows[f"sector_{key}"] = float(group == key)
    for slug in strategies:
        rows[f"strategy_{slug}"] = float(slug == strategy)
    return rows


# ---------------------------------------------------------------- dataset

def build_events(prices_by_symbol: Mapping[str, pd.DataFrame], spy_prices: pd.DataFrame,
                 barrier: BarrierConfig | None = None,
                 strategies: tuple[str, ...] = REPLAYABLE_STRATEGIES,
                 start_date: str | None = None) -> pd.DataFrame:
    """One labelled row per primary BUY (decision on the close of bar t)."""
    barrier = barrier or BarrierConfig()
    names = feature_names(strategies)
    parts = []
    for symbol, prices in sorted(prices_by_symbol.items()):
        frame = prices.sort_values("date").reset_index(drop=True)
        market, signals = _market_and_signals(frame, spy_prices)
        symbol_parts = []
        for slug in strategies:
            signal = signals[slug]
            positions = np.flatnonzero(signal["action"].to_numpy() == "BUY")
            if start_date is not None:
                positions = positions[market["date"].to_numpy()[positions] >= np.datetime64(start_date)]
            labels = label_events(frame, positions, barrier)
            if labels.empty:
                continue
            design = _design_rows(market, symbol, slug, signal["confidence"], signal["probability_up"], strategies)
            rows = design.iloc[labels["event_idx"].to_numpy()].reset_index(drop=True)
            rows = rows.dropna(subset=list(MARKET_FEATURES))
            labels = labels.loc[rows.index].reset_index(drop=True)
            rows = rows.reset_index(drop=True)
            rows["symbol"] = symbol.upper()
            rows["strategy"] = slug
            for column in ("event_idx", "entry_idx", "exit_idx", "barrier", "net_return", "label",
                           "take_profit_pct", "stop_loss_pct", "holding_days"):
                rows[column] = labels[column].to_numpy()
            rows["t0"] = _normalize_dates(labels["decision_date"]).to_numpy()
            rows["t1"] = _normalize_dates(labels["exit_date"]).to_numpy()
            symbol_parts.append(rows)
        if symbol_parts:
            events = pd.concat(symbol_parts, ignore_index=True)
            events["uniqueness"] = average_uniqueness(events["entry_idx"], events["exit_idx"])
            parts.append(events)
    if not parts:
        return pd.DataFrame(columns=[*names, "symbol", "strategy", "t0", "t1", "label", "net_return", "uniqueness"])
    events = pd.concat(parts, ignore_index=True).sort_values(["t0", "symbol", "strategy"]).reset_index(drop=True)
    return events


# ---------------------------------------------------------------- models

def make_classifier(kind: str = "gradient_boosting", seed: int = 7):
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    if kind == "gradient_boosting":
        return HistGradientBoostingClassifier(
            max_iter=200, learning_rate=0.05, max_leaf_nodes=15, min_samples_leaf=200,
            l2_regularization=1.0, random_state=seed,
        )
    if kind == "logistic_regression":
        return make_pipeline(StandardScaler(), LogisticRegression(C=0.1, max_iter=2000, random_state=seed))
    raise ValueError(f"Unknown classifier kind: {kind}")


def _fit(model, x: pd.DataFrame, y, weights):
    if hasattr(model, "steps"):  # pipeline: route the weight to the final step
        model.fit(x, y, **{f"{model.steps[-1][0]}__sample_weight": weights})
    else:
        model.fit(x, y, sample_weight=weights)
    return model


def fit_calibrator(raw: np.ndarray, target: np.ndarray, weights: np.ndarray | None = None, method: str = "isotonic"):
    """Isotonic (default) or Platt calibrator fitted on a held-out fold."""
    from sklearn.isotonic import IsotonicRegression
    from sklearn.linear_model import LogisticRegression

    raw = np.asarray(raw, dtype=float)
    if method == "isotonic":
        calibrator = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip", increasing=True)
        calibrator.fit(raw, target, sample_weight=weights)
        return ("isotonic", calibrator)
    if method == "platt":
        logit = np.log(np.clip(raw, 1e-6, 1 - 1e-6) / (1 - np.clip(raw, 1e-6, 1 - 1e-6))).reshape(-1, 1)
        calibrator = LogisticRegression(C=1e6, max_iter=1000).fit(logit, target, sample_weight=weights)
        return ("platt", calibrator)
    raise ValueError(f"Unknown calibration method: {method}")


def apply_calibrator(calibrator, raw: np.ndarray) -> np.ndarray:
    kind, model = calibrator
    raw = np.asarray(raw, dtype=float)
    if kind == "isotonic":
        return np.clip(model.predict(raw), 0.0, 1.0)
    logit = np.log(np.clip(raw, 1e-6, 1 - 1e-6) / (1 - np.clip(raw, 1e-6, 1 - 1e-6))).reshape(-1, 1)
    return model.predict_proba(logit)[:, 1]


# ---------------------------------------------------------------- thresholds and metrics

def trade_metrics(net_returns: np.ndarray) -> dict:
    net = np.asarray(net_returns, dtype=float)
    n = len(net)
    if not n:
        return {"trades": 0, "win_rate": None, "expectancy": None, "profit_factor": None,
                "avg_win": None, "avg_loss": None, "total_return": 0.0}
    wins, losses = net[net > 0], net[net <= 0]
    gross_loss = float(-losses.sum())
    return {
        "trades": int(n),
        "win_rate": float(len(wins) / n),
        "expectancy": float(net.mean()),
        "profit_factor": float(wins.sum() / gross_loss) if gross_loss > 0 else (math.inf if len(wins) else None),
        "avg_win": float(wins.mean()) if len(wins) else None,
        "avg_loss": float(losses.mean()) if len(losses) else None,
        "total_return": float(net.sum()),
    }


@dataclass(frozen=True)
class ThresholdPolicy:
    min_win_rate: float = 0.80
    min_trades: int = 30
    # "expectancy" maximises mean net return per trade; "total" maximises the
    # sum (expectancy x trades).  Both require win rate >= min_win_rate,
    # trades >= min_trades and expectancy > 0.
    objective: str = "expectancy"
    grid_size: int = 200


def select_threshold(probability: np.ndarray, net_returns: np.ndarray,
                     policy: ThresholdPolicy = ThresholdPolicy()) -> dict:
    """Pick tau on a validation fold; ``threshold`` is None when infeasible."""
    probability = np.asarray(probability, dtype=float)
    net = np.asarray(net_returns, dtype=float)
    if len(probability) != len(net):
        raise ValueError("probability and returns must align")
    if policy.objective not in {"expectancy", "total"}:
        raise ValueError(f"Unknown objective: {policy.objective}")
    candidates = np.unique(np.quantile(probability, np.linspace(0, 1, policy.grid_size + 1))) if len(probability) else []
    best = None
    frontier = []
    for tau in candidates:
        kept = net[probability >= tau]
        if len(kept) < policy.min_trades:
            continue
        win_rate, expectancy = float((kept > 0).mean()), float(kept.mean())
        frontier.append({"threshold": float(tau), "trades": int(len(kept)), "win_rate": win_rate, "expectancy": expectancy})
        if win_rate < policy.min_win_rate or expectancy <= 0:
            continue
        score = expectancy if policy.objective == "expectancy" else expectancy * len(kept)
        if best is None or score > best[0] + 1e-15:
            best = (score, float(tau), int(len(kept)), win_rate, expectancy)
    if best is None:
        max_win = max((row["win_rate"] for row in frontier), default=None)
        return {"threshold": None, "feasible": False, "validation_trades": 0, "validation_win_rate": None,
                "validation_expectancy": None, "best_achievable_win_rate": max_win, "frontier": frontier}
    return {"threshold": best[1], "feasible": True, "validation_trades": best[2], "validation_win_rate": best[3],
            "validation_expectancy": best[4], "best_achievable_win_rate": max(r["win_rate"] for r in frontier),
            "frontier": frontier}


def cluster_bootstrap(net_returns: np.ndarray, clusters: np.ndarray, samples: int = 2000, seed: int = 11) -> dict:
    """95% CIs for win rate and expectancy, resampling whole clusters (e.g. months).

    Overlapping and same-day trades are correlated; resampling clusters keeps
    that dependence instead of pretending every trade is independent.
    """
    net = np.asarray(net_returns, dtype=float)
    if len(net) < 2:
        return {"win_rate_ci": None, "expectancy_ci": None, "clusters": int(len(set(clusters)))}
    keys, inverse = np.unique(np.asarray(clusters), return_inverse=True)
    wins = np.bincount(inverse, weights=(net > 0).astype(float), minlength=len(keys))
    sums = np.bincount(inverse, weights=net, minlength=len(keys))
    counts = np.bincount(inverse, minlength=len(keys)).astype(float)
    rng = np.random.default_rng(seed)
    draws = rng.integers(0, len(keys), size=(samples, len(keys)))
    n = counts[draws].sum(axis=1)
    win_rate = wins[draws].sum(axis=1) / n
    expectancy = sums[draws].sum(axis=1) / n
    return {
        "win_rate_ci": [float(np.quantile(win_rate, 0.025)), float(np.quantile(win_rate, 0.975))],
        "expectancy_ci": [float(np.quantile(expectancy, 0.025)), float(np.quantile(expectancy, 0.975))],
        "clusters": int(len(keys)),
    }


def executable_trades(events: pd.DataFrame, take: np.ndarray) -> pd.DataFrame:
    """Non-overlapping trades per (symbol, strategy): enter only when flat.

    Mirrors the paper path's no-pyramiding rule: a strategy already holding a
    symbol cannot add a second lot on a persistent BUY.
    """
    frame = events.assign(_take=np.asarray(take, dtype=bool)).sort_values(["symbol", "strategy", "event_idx"])
    keep = np.zeros(len(frame), dtype=bool)
    sym, strat = frame["symbol"].to_numpy(), frame["strategy"].to_numpy()
    entry, exit_, wanted = frame["entry_idx"].to_numpy(), frame["exit_idx"].to_numpy(), frame["_take"].to_numpy()
    busy_until, last_key = -1, None
    for i in range(len(frame)):
        key = (sym[i], strat[i])
        if key != last_key:
            busy_until, last_key = -1, key
        if wanted[i] and entry[i] > busy_until:
            keep[i] = True
            busy_until = exit_[i]
    return frame.loc[keep].drop(columns="_take")


# ---------------------------------------------------------------- walk-forward

@dataclass
class WalkForwardConfig:
    first_test_year: int = 2014
    last_test_year: int | None = None
    embargo_days: int = 10
    model_kind: str = "gradient_boosting"
    calibration: str = "isotonic"
    policy: ThresholdPolicy = field(default_factory=ThresholdPolicy)
    seed: int = 7


def three_way_split(events: pd.DataFrame, test_start: pd.Timestamp, embargo_days: int) -> dict:
    """Purged/embargoed train | calibration | validation partitions before ``test_start``.

    validation = decisions in [test_start - 1y, test_start) whose labels end
    before ``test_start - embargo``; calibration = the year before that with the
    same purge against the validation start; training = every earlier event
    whose label ends before ``calibration_start - embargo``.
    """
    embargo = pd.Timedelta(days=embargo_days)
    validation_start = test_start - pd.DateOffset(years=1)
    calibration_start = test_start - pd.DateOffset(years=2)
    t0, t1 = events["t0"], events["t1"]
    validation = events.loc[(t0 >= validation_start) & (t0 < test_start) & (t1 < test_start - embargo)]
    calibration = events.loc[(t0 >= calibration_start) & (t0 < validation_start) & (t1 < validation_start - embargo)]
    training = events.loc[(t0 < calibration_start) & (t1 < calibration_start - embargo)]
    return {"training": training, "calibration": calibration, "validation": validation,
            "calibration_start": calibration_start, "validation_start": validation_start}


def fit_fold(training: pd.DataFrame, calibration: pd.DataFrame, validation: pd.DataFrame,
             names: list[str], config: WalkForwardConfig) -> dict:
    if training["label"].nunique() < 2 or calibration["label"].nunique() < 2:
        raise ValueError("training and calibration folds need both classes")
    model = _fit(make_classifier(config.model_kind, config.seed), training[names], training["label"].to_numpy(),
                 training["uniqueness"].to_numpy())
    raw_calibration = model.predict_proba(calibration[names])[:, 1]
    calibrator = fit_calibrator(raw_calibration, calibration["label"].to_numpy(),
                                calibration["uniqueness"].to_numpy(), config.calibration)
    validation_probability = apply_calibrator(calibrator, model.predict_proba(validation[names])[:, 1])
    selection = select_threshold(validation_probability, validation["net_return"].to_numpy(), config.policy)
    return {"model": model, "calibrator": calibrator, "selection": selection,
            "validation_probability": validation_probability}


def walk_forward(events: pd.DataFrame, config: WalkForwardConfig | None = None,
                 strategies: tuple[str, ...] = REPLAYABLE_STRATEGIES) -> dict:
    """Yearly purged walk-forward; returns OOS predictions and per-fold audit."""
    config = config or WalkForwardConfig()
    names = feature_names(strategies)
    last_year = config.last_test_year or int(events["t0"].max().year)
    predictions, audit, validation = [], [], {}
    for year in range(config.first_test_year, last_year + 1):
        test_start = pd.Timestamp(year=year, month=1, day=1)
        split = three_way_split(events, test_start, config.embargo_days)
        test = events.loc[(events["t0"] >= test_start) & (events["t0"] < test_start + pd.DateOffset(years=1))]
        if test.empty or len(split["training"]) < 1000 or len(split["calibration"]) < 200 or len(split["validation"]) < 200:
            continue
        fold = fit_fold(split["training"], split["calibration"], split["validation"], names, config)
        test_probability = apply_calibrator(fold["calibrator"], fold["model"].predict_proba(test[names])[:, 1])
        selection = fold["selection"]
        threshold = selection["threshold"]
        out = test[["t0", "t1", "symbol", "strategy", "event_idx", "entry_idx", "exit_idx", "label", "net_return"]].copy()
        out["probability"] = test_probability
        out["threshold"] = np.nan if threshold is None else threshold
        out["take"] = (test_probability >= threshold) if threshold is not None else False
        out["fold"] = year
        predictions.append(out)
        validation[year] = (fold["validation_probability"], split["validation"]["net_return"].to_numpy())
        audit.append({
            "test_year": year,
            "training_rows": int(len(split["training"])),
            "training_last_label_end": str(split["training"]["t1"].max().date()),
            "calibration_rows": int(len(split["calibration"])),
            "calibration_window": [str(split["calibration_start"].date()), str(split["validation_start"].date())],
            "calibration_last_label_end": str(split["calibration"]["t1"].max().date()),
            "validation_rows": int(len(split["validation"])),
            "validation_last_label_end": str(split["validation"]["t1"].max().date()),
            "test_rows": int(len(test)),
            "threshold": threshold,
            "feasible": selection["feasible"],
            "validation_trades": selection["validation_trades"],
            "validation_win_rate": selection["validation_win_rate"],
            "validation_expectancy": selection["validation_expectancy"],
            "validation_best_achievable_win_rate": selection["best_achievable_win_rate"],
        })
    frame = pd.concat(predictions, ignore_index=True) if predictions else pd.DataFrame()
    return {"predictions": frame, "audit": audit, "validation": validation}


def reselect(result: dict, policy: ThresholdPolicy) -> tuple[np.ndarray, dict]:
    """Re-run per-fold threshold selection (validation data only) under ``policy``.

    Lets one walk-forward fit be evaluated at several win-rate targets without
    touching test labels: each fold's threshold comes from its own validation
    year, then is applied once to its test year.
    """
    predictions = result["predictions"]
    take = np.zeros(len(predictions), dtype=bool)
    thresholds = {}
    for year, (probability, returns) in result["validation"].items():
        selection = select_threshold(probability, returns, policy)
        thresholds[year] = selection["threshold"]
        if selection["threshold"] is not None:
            mask = (predictions["fold"] == year).to_numpy()
            take[mask] = predictions.loc[mask, "probability"].to_numpy() >= selection["threshold"]
    return take, thresholds


def summarize(predictions: pd.DataFrame, take: np.ndarray | None = None, events: pd.DataFrame | None = None) -> dict:
    """Event-level and executable (non-overlapping) OOS metrics with month-cluster CIs."""
    take = predictions["take"].to_numpy(bool) if take is None else np.asarray(take, dtype=bool)
    kept = predictions.loc[take]
    months = kept["t0"].dt.to_period("M").astype(str).to_numpy()
    executable = executable_trades(predictions, take)
    exec_months = executable["t0"].dt.to_period("M").astype(str).to_numpy()
    return {
        "events": trade_metrics(kept["net_return"].to_numpy()) | cluster_bootstrap(kept["net_return"].to_numpy(), months),
        "executable": trade_metrics(executable["net_return"].to_numpy())
        | cluster_bootstrap(executable["net_return"].to_numpy(), exec_months),
    }


def frontier(predictions: pd.DataFrame, thresholds: Iterable[float]) -> list[dict]:
    """OOS win-rate / expectancy at fixed calibrated-probability thresholds."""
    rows = []
    for tau in thresholds:
        take = predictions["probability"].to_numpy() >= tau
        executable = executable_trades(predictions, take)
        rows.append({"threshold": float(tau), "events": trade_metrics(predictions.loc[take, "net_return"].to_numpy()),
                     "executable": trade_metrics(executable["net_return"].to_numpy())})
    return rows


# ---------------------------------------------------------------- final model + persistence

def fit_final_model(events: pd.DataFrame, config: WalkForwardConfig | None = None,
                    strategies: tuple[str, ...] = REPLAYABLE_STRATEGIES, barrier: BarrierConfig | None = None,
                    data_end: str | None = None) -> dict:
    """Train on everything, anchored so the newest year is the validation fold."""
    config = config or WalkForwardConfig()
    names = feature_names(strategies)
    last_label = events["t1"].max()
    anchor = (pd.Timestamp(data_end) if data_end else last_label) + pd.Timedelta(days=config.embargo_days + 1)
    split = three_way_split(events, anchor, config.embargo_days)
    fold = fit_fold(split["training"], split["calibration"], split["validation"], names, config)
    selection = {key: value for key, value in fold["selection"].items() if key != "frontier"}
    return {
        "format": MODEL_FORMAT,
        "model_kind": config.model_kind,
        "calibration": config.calibration,
        "feature_names": names,
        "strategies": list(strategies),
        "barrier": (barrier or BarrierConfig()).to_dict(),
        "policy": {"min_win_rate": config.policy.min_win_rate, "min_trades": config.policy.min_trades,
                   "objective": config.policy.objective},
        "threshold": selection["threshold"],
        "selection": selection,
        "frontier": fold["selection"]["frontier"],
        "training_window": {
            "training_rows": int(len(split["training"])),
            "training_last_label_end": str(split["training"]["t1"].max().date()),
            "calibration_window": [str(split["calibration_start"].date()), str(split["validation_start"].date())],
            "calibration_rows": int(len(split["calibration"])),
            "validation_window_end": str(anchor.date()),
            "validation_rows": int(len(split["validation"])),
        },
        "model": fold["model"],
        "calibrator": fold["calibrator"],
    }


def save_model(bundle: dict, path: str | Path) -> dict:
    """Write ``<path>`` (joblib) and ``<path>.json`` (manifest with sha256)."""
    import joblib

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, path, compress=3)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {key: value for key, value in bundle.items() if key not in {"model", "calibrator"}}
    manifest["sha256"] = digest
    Path(f"{path}.json").write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n")
    return manifest


_MODEL_CACHE: dict[tuple, dict] = {}


def load_model(path: str | Path) -> dict:
    """Load a bundle; verifies the manifest digest when one exists. Raises on any problem."""
    import joblib

    path = Path(path)
    stat = path.stat()
    cache_key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    cached = _MODEL_CACHE.get(cache_key)
    if cached is not None:
        return cached
    payload = path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    manifest_path = Path(f"{path}.json")
    if manifest_path.exists():
        expected = json.loads(manifest_path.read_text()).get("sha256")
        if expected and digest != expected:
            raise ValueError("meta-label model digest does not match its manifest")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        bundle = joblib.load(io.BytesIO(payload))
    if not isinstance(bundle, dict) or bundle.get("format") != MODEL_FORMAT:
        raise ValueError("unsupported meta-label model format")
    for required in ("model", "calibrator", "feature_names", "strategies"):
        if required not in bundle:
            raise ValueError(f"meta-label model is missing {required}")
    bundle = {**bundle, "sha256": digest}
    _MODEL_CACHE.clear()
    _MODEL_CACHE[cache_key] = bundle
    return bundle


def predict_probability(bundle: dict, rows: pd.DataFrame) -> np.ndarray:
    raw = bundle["model"].predict_proba(rows[bundle["feature_names"]])[:, 1]
    return apply_calibrator(bundle["calibrator"], raw)


def latest_market_features(prices: pd.DataFrame, spy_prices: pd.DataFrame | None) -> pd.DataFrame | None:
    """Market features of the newest bar (one row), or None with too little history."""
    if prices is None or len(prices) < MAX_FEATURE_LOOKBACK:
        return None
    frame = prices.sort_values("date").reset_index(drop=True) if "date" in prices else prices.reset_index(drop=True)
    return market_features(frame, spy_prices).iloc[[-1]].reset_index(drop=True)


def score_buy_signal(bundle: dict, symbol: str, strategy: str, confidence: float, probability_up: float,
                     prices: pd.DataFrame | None, spy_prices: pd.DataFrame | None, *,
                     latest: pd.DataFrame | None = None) -> dict:
    """Calibrated probability and take/skip for the latest bar of ``prices``.

    ``latest`` may carry ``latest_market_features(prices, spy_prices)`` so
    several strategies on one symbol share one feature computation.
    """
    result: dict[str, Any] = {"threshold": bundle.get("threshold"), "model_sha256": bundle.get("sha256")}
    if strategy not in bundle["strategies"]:
        return result | {"status": "unsupported_strategy", "probability": None, "decision": None,
                         "reason": f"meta-label model was not trained on {strategy}"}
    market = latest if latest is not None else latest_market_features(prices, spy_prices)
    if market is None:
        return result | {"status": "insufficient_history", "probability": None, "decision": None,
                         "reason": f"need {MAX_FEATURE_LOOKBACK} daily bars, have {0 if prices is None else len(prices)}"}
    rows = _design_rows(market, symbol, strategy, [confidence], [probability_up], bundle["strategies"])
    missing = [name for name in MARKET_FEATURES if not np.isfinite(float(rows[name].iloc[0]))]
    if missing:
        return result | {"status": "missing_features", "probability": None, "decision": None,
                         "reason": f"non-finite features: {', '.join(missing)}"}
    probability = float(predict_probability(bundle, rows)[0])
    threshold = bundle.get("threshold")
    decision = "take" if threshold is not None and probability >= threshold else "skip"
    reason = ("no validated threshold met the win-rate target; every signal is skipped" if threshold is None
              else f"calibrated probability {probability:.3f} {'>=' if decision == 'take' else '<'} threshold {threshold:.3f}")
    feature_date = market["date"].iloc[-1]
    return result | {"status": "scored", "probability": round(probability, 6), "decision": decision, "reason": reason,
                     "feature_date": str(feature_date.date()) if pd.notna(feature_date) else None}
