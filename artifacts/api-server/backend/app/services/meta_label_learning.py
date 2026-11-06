"""Self-retraining for the meta-label BUY filter (paper only).

The filter used to be trained once, offline, and never again.  This module
retrains it off hours on two sources:

* replayed history: every primary BUY over free daily bars since
  ``meta_label_history_start``, labelled with each strategy's real exit;
* the bot's own closed paper round trips (scheduled and exploration), which
  replace the simulated label of the same decision with the realised net
  result and carry ``meta_label_paper_outcome_weight`` times the weight.

A retrain is due when the artifact is missing, older than
``meta_label_retrain_max_age_days``, or at least
``meta_label_retrain_min_new_outcomes`` paper trades closed since it was
trained.  The candidate replaces the incumbent unless it has no feasible
threshold while the incumbent has one.  The previous artifact is kept as
``<path>.prev``.  Promotion to enforce stays with ``meta_label_modes``, which
needs live shadow evidence; nothing here places, blocks or sizes an order.
"""
from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

import pandas as pd
from sqlalchemy.orm import Session

from app.core.config import settings
from app.services import meta_label
from app.services.audit import write_audit_log
from app.services.meta_label_shadow import BACKEND_ROOT, model_path

logger = logging.getLogger(__name__)
UTC = timezone.utc
PAPER_SOURCES = ("scheduled_round_trip", "exploration_round_trip")


def history_dir() -> Path:
    configured = (settings.meta_label_history_dir or "").strip()
    if not configured:
        return BACKEND_ROOT / "stock_training_artifacts" / "meta_label" / "history"
    path = Path(configured)
    return path if path.is_absolute() else BACKEND_ROOT / path


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def paper_outcomes(db: Session, *, since: Optional[datetime] = None) -> list[dict]:
    """Closed paper round trips as ``merge_paper_outcomes`` inputs (net when known)."""
    from app.services.trade_attribution import current_attributions

    out = []
    for row in current_attributions(db, since=since, sources=PAPER_SOURCES):
        quality = (row.detail or {}).get("signal_quality") or {}
        out.append({
            "symbol": row.symbol, "strategy": row.strategy_type,
            "opened_at": _utc(row.opened_at).isoformat(), "closed_at": _utc(row.closed_at).isoformat(),
            "net_return": float(row.net_return if row.net_return is not None else row.gross_return),
            "confidence": quality.get("confidence"), "probability_up": quality.get("probability_up"),
        })
    return out


def _manifest(path: Path) -> Optional[dict]:
    import json

    manifest = Path(f"{path}.json")
    if not path.exists() or not manifest.exists():
        return None
    try:
        return json.loads(manifest.read_text())
    except ValueError:
        return None


def retrain_due(db: Session, *, now: datetime) -> tuple[bool, str]:
    manifest = _manifest(model_path())
    if manifest is None:
        return True, "no meta-label artifact"
    trained_at = manifest.get("trained_at")
    if not trained_at:
        return True, "artifact predates self-retraining"
    trained = _utc(datetime.fromisoformat(trained_at))
    if now - trained >= timedelta(days=settings.meta_label_retrain_max_age_days):
        return True, f"artifact is older than {settings.meta_label_retrain_max_age_days} days"
    last_closed = (manifest.get("paper") or {}).get("last_closed_at")
    since = _utc(datetime.fromisoformat(last_closed)) + timedelta(microseconds=1) if last_closed else None
    fresh = len(paper_outcomes(db, since=since))
    if fresh >= settings.meta_label_retrain_min_new_outcomes:
        return True, f"{fresh} paper trades closed since the last training"
    return False, f"{fresh} new paper trades (< {settings.meta_label_retrain_min_new_outcomes}) and artifact is fresh"


def load_history(symbols: list[str], *, download: bool = True,
                 loader: Optional[Callable[[str], pd.DataFrame]] = None) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """Daily adjusted bars per symbol from the cache (refreshed when ``download``)."""
    from app.services.strategy_scorecard import load_daily_history, normalize_prices

    prices, missing = {}, []
    today = datetime.now(UTC).date().isoformat()
    for symbol in symbols:
        try:
            frame = loader(symbol) if loader else load_daily_history(symbol, history_dir(), download=download)
            frame = normalize_prices(frame, start=settings.meta_label_history_start, end=today)
        except Exception as exc:  # one bad symbol must not stop the retrain
            logger.warning("meta-label history unavailable for %s: %s", symbol, exc)
            missing.append(symbol)
            continue
        if len(frame) >= meta_label.MAX_FEATURE_LOOKBACK:
            prices[symbol] = frame
        else:
            missing.append(symbol)
    return prices, missing


def _feasible(bundle_or_manifest: Optional[dict]) -> bool:
    return bool(bundle_or_manifest) and bundle_or_manifest.get("threshold") is not None


def retrain_meta_label(db: Session, *, now: Optional[datetime] = None, force: bool = False,
                       loader: Optional[Callable[[str], pd.DataFrame]] = None) -> dict:
    now = now or datetime.now(UTC)
    base = {"paper_only": True, "live_authorized": False}
    if not settings.meta_label_retrain_enabled and not force:
        return base | {"status": "disabled"}
    due, reason = retrain_due(db, now=now)
    if not due and not force:
        return base | {"status": "not_due", "reason": reason}

    symbols = sorted({symbol.upper() for symbol in settings.paper_execution_symbols} | {"SPY"})
    prices, missing = load_history(symbols, loader=loader)
    if "SPY" not in prices:
        return base | {"status": "blocked", "reason": "SPY history is required for regime features",
                       "missing_symbols": missing}
    events = meta_label.build_events(prices, prices["SPY"])
    outcomes = paper_outcomes(db)
    events, merged = meta_label.merge_paper_outcomes(events, outcomes, prices, prices["SPY"],
                                                     weight=settings.meta_label_paper_outcome_weight)
    data_end = max(str(frame["date"].iloc[-1])[:10] for frame in prices.values())
    policy = meta_label.ThresholdPolicy(min_win_rate=settings.meta_label_min_win_rate,
                                        objective=settings.meta_label_objective)
    candidate = meta_label.fit_final_model(events, meta_label.WalkForwardConfig(policy=policy), data_end=data_end)
    last_closed = max((row["closed_at"] for row in outcomes), default=None)
    candidate["trained_at"] = now.isoformat()
    candidate["trigger"] = "forced" if force and not due else reason
    candidate["data"] = {"symbols": sorted(prices), "missing_symbols": missing,
                         "start": settings.meta_label_history_start, "data_end": data_end,
                         "events": int(len(events)), "source": "yfinance daily, auto_adjust=True"}
    candidate["paper"] = merged | {"last_closed_at": last_closed,
                                   "weight": settings.meta_label_paper_outcome_weight}

    path = model_path()
    incumbent = _manifest(path)
    selection = {key: value for key, value in candidate["selection"].items() if key != "frontier"}
    summary = {"trigger": candidate["trigger"], "threshold": candidate["threshold"], "selection": selection,
               "events": int(len(events)), "paper": candidate["paper"], "missing_symbols": missing,
               "incumbent_sha256": (incumbent or {}).get("sha256"),
               "incumbent_threshold": (incumbent or {}).get("threshold")}
    if not _feasible(candidate) and _feasible(incumbent):
        status, message = "kept_incumbent", "candidate has no feasible threshold; the incumbent does"
    else:
        if path.exists():
            shutil.copy2(path, f"{path}.prev")
            if Path(f"{path}.json").exists():
                shutil.copy2(f"{path}.json", f"{path}.prev.json")
        staged = path.with_name(f"{path.name}.staged")
        manifest = meta_label.save_model(candidate, staged)
        staged.replace(path)
        Path(f"{staged}.json").replace(f"{path}.json")
        summary["sha256"] = manifest["sha256"]
        status = "promoted"
        message = (f"threshold {candidate['threshold']:.3f}, validation win rate "
                   f"{selection['validation_win_rate']:.1%}, expectancy {selection['validation_expectancy']:.4%} "
                   f"over {selection['validation_trades']} trades" if _feasible(candidate)
                   else "no feasible threshold at the win-rate floor; every BUY stays skip")
    write_audit_log(db, event_type="meta_label_retrain", action=status, status="complete",
                    message=f"meta-label retrain: {message}", entity_type="meta_label_model",
                    payload=meta_label_json(summary) | {"paper_only": True})
    logger.info("meta-label retrain %s: %s", status, message)
    return base | {"status": status, "message": message, **meta_label_json(summary)}


def meta_label_json(value):
    """JSON-safe copy (numpy scalars, NaN/inf -> None)."""
    import math

    import numpy as np

    if isinstance(value, dict):
        return {str(key): meta_label_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [meta_label_json(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return None if not math.isfinite(float(value)) else float(value)
    return value
