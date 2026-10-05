"""Delayed test-then-train research. No broker, registry promotion or orders."""
from datetime import datetime, timedelta, timezone
from math import isfinite, log

import numpy as np
from sklearn.linear_model import SGDClassifier
from sqlalchemy.orm import Session

from app.models import IntradayBar, OnlineResearchForecast
from app.services.intraday_data import ALLOWED_SYMBOLS, NY, _aware_utc, session_bounds
from app.services import online_shadow_returns as shadow
from app.services import online_return_challenger as returns
from app.services import return_evaluation_cohort as return_cohort

VERSION = "iex-sgd-v1"
WINDOW = 1000
UTC = timezone.utc
COMPARISON_VERSION = "iex-direction-comparison-v1"
COMPARATORS = ("online_sgd", "momentum_5m", "reversal_5m", "historical", "neutral")


def _comparators(features, probability, baseline):
    # Fixed weak forecasts, not calibrated probabilities or executable signals.
    momentum = 0.6 if features[2] > 0.05 else 0.4 if features[2] < -0.05 else 0.5
    return {"version": COMPARISON_VERSION, "threshold_percent": 0.05,
            "probabilities": dict(zip(COMPARATORS, (probability, momentum, 1-momentum, baseline, 0.5)))}


def _comparison_scores(prediction, label):
    declared = prediction.get("comparators")
    if not isinstance(declared, dict) or declared.get("version") != COMPARISON_VERSION:
        return None
    if declared != _comparators(prediction["features"], prediction["probability_up"], prediction["baseline_up"]):
        return None
    return {"version": COMPARISON_VERSION,
            "scores": {name: _loss(p, label) for name, p in declared["probabilities"].items()}}


def _bars(db, symbol):
    return db.query(IntradayBar).filter_by(symbol=symbol, provider="alpaca_iex", feed_class="iex", timeframe="1m")


def _forecasts(db, symbol):
    return db.query(OnlineResearchForecast).filter_by(symbol=symbol, version=VERSION)


def _model(rows):
    model = SGDClassifier(loss="log_loss", learning_rate="constant", eta0=0.01, random_state=42, shuffle=False)
    for row in rows:
        model.partial_fit([row.prediction["features"]], [row.outcome["label"]], classes=np.array([0, 1]))
    return model


def _loss(p, label):
    p = max(1e-12, min(1 - 1e-12, p))
    return {"brier": (p - label) ** 2, "log_loss": -log(p if label else 1 - p)}


def _valid_prediction(prediction):
    try:
        features = prediction["features"]
        probability, baseline = prediction["probability_up"], prediction["baseline_up"]
        source = prediction["source_close"]
        return (isinstance(features, list) and len(features) == 3
                and all(type(v) in (int, float) and isfinite(v) and -5 <= v <= 5 for v in features)
                and all(type(v) in (int, float) and isfinite(v) and 0 <= v <= 1 for v in (probability, baseline))
                and type(source) in (int, float) and isfinite(source) and source > 0)
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def _known_outcome(row, now):
    if not _valid_prediction(row.prediction):
        return False
    try:
        prediction, outcome = row.prediction, row.outcome
        label = outcome["label"]
        probability, baseline = prediction["probability_up"], prediction["baseline_up"]
        source, close = prediction["source_close"], outcome["close"]
        if (type(close) not in (int, float) or not isfinite(close) or close <= 0
                or type(label) is not int or label not in (0, 1)
                or label != int(close > source)
                or outcome["model"] != _loss(probability, label)
                or outcome["baseline"] != _loss(baseline, label)):
            return False
        observed = datetime.fromisoformat(row.outcome["observed_at"])
        return (observed.tzinfo is not None
                and _aware_utc(row.issued_at) < _aware_utc(row.target_at)
                and _aware_utc(row.target_at) + timedelta(minutes=1) <= observed <= now)
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def advance_online_research(db: Session, *, now=None):
    """Caller owns transaction and the collector's shared job lease."""
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("Research clock must be timezone aware")
    results = []
    for symbol in sorted(ALLOWED_SYMBOLS):
        query = _forecasts(db, symbol)
        scored = expired = invalid = 0
        for row in query.filter_by(status="pending").order_by(OnlineResearchForecast.target_at).all():
            if not _valid_prediction(row.prediction):
                row.status = "invalid"
                invalid += 1
                continue
            target = _aware_utc(row.target_at)
            if now < target + timedelta(minutes=2):
                continue
            bar = _bars(db, symbol).filter(IntradayBar.opened_at == target).first()
            # A fixed observation deadline prevents a later backfill from turning
            # a missed outcome into selectively recovered forward evidence.
            deadline = target + timedelta(minutes=16)
            price = float(bar.close) if bar else None
            if (bar and price is not None and isfinite(price) and price > 0
                    and target + timedelta(minutes=1) <= _aware_utc(bar.ingested_at) <= min(now, deadline)):
                label = int(price > row.prediction["source_close"])
                row.outcome = {
                    "label": label, "close": price, "observed_at": now.isoformat(),
                    "model": _loss(row.prediction["probability_up"], label),
                    "baseline": _loss(row.prediction["baseline_up"], label),
                }
                comparison = _comparison_scores(row.prediction, label)
                if comparison is not None:
                    row.outcome = {**row.outcome, "comparators": comparison}
                    declaration = row.prediction.get("shadow")
                    if declaration is not None and declaration == shadow.plan(row.issued_at, row.target_at, row.prediction["comparators"]["probabilities"]):
                        entry_at = datetime.fromisoformat(declaration["entry_at"])
                        entry = _bars(db, symbol).filter(IntradayBar.opened_at == entry_at).first()
                        row.outcome = {**row.outcome, "shadow": shadow.observe(declaration, entry, price, now=now)}
                row.status = "scored"
                scored += 1
            elif now >= deadline:
                row.status = "expired"
                expired += 1
        db.flush()
        bars = list(reversed(_bars(db, symbol).filter(
            IntradayBar.opened_at <= now - timedelta(minutes=2),
            IntradayBar.ingested_at <= now,
        ).order_by(IntradayBar.opened_at.desc()).limit(6).all()))
        reason = "waiting_for_fresh_contiguous_bars"
        if len(bars) == 6:
            source = _aware_utc(bars[-1].opened_at)
            target = source + timedelta(minutes=10)
            bounds = session_bounds(source.astimezone(NY).date())
            contiguous = all(_aware_utc(bar.opened_at) == source - timedelta(minutes=5-i) for i, bar in enumerate(bars))
            completed = all(_aware_utc(bar.ingested_at) >= _aware_utc(bar.opened_at) + timedelta(minutes=1) for bar in bars)
            eligible = (contiguous and completed and bounds and bounds[0] <= _aware_utc(bars[0].opened_at)
                        and target + timedelta(minutes=1) <= bounds[1]
                        and timedelta(minutes=2) <= now-source <= timedelta(minutes=3)
                        and now < target)
            if eligible and not query.filter_by(source_at=source).first():
                closes = [float(bar.close) for bar in bars]
                if all(isfinite(p) and p > 0 for p in closes):
                    # Fixed percent units avoid fitting a scaler on future data.
                    features = [max(-5., min(5., 100*(closes[-1]/closes[-1-lag]-1))) for lag in (1, 3, 5)]
                    history = list(reversed(query.filter_by(status="scored").order_by(OnlineResearchForecast.id.desc()).limit(WINDOW).all()))
                    history = [row for row in history if _known_outcome(row, now)]
                    model = _model(history)
                    probability = float(model.predict_proba([features])[0, 1]) if history else 0.5
                    baseline = (1 + sum(row.outcome["label"] for row in history))/(2+len(history))
                    if not isfinite(probability):
                        raise ValueError("Nonfinite online prediction")
                    comparators = _comparators(features, probability, baseline)
                    db.add(OnlineResearchForecast(
                        symbol=symbol, version=VERSION, source_at=source, issued_at=now,
                        target_at=target, status="pending", prediction={
                            "features": features, "source_close": closes[-1],
                            "probability_up": probability, "baseline_up": baseline,
                            "training_examples": len(history), "provider": "alpaca_iex",
                            "feed_class": "iex", "horizon_minutes": 10,
                            "source_ingested_at": _aware_utc(bars[-1].ingested_at).isoformat(),
                            "comparators": comparators,
                            "shadow": shadow.plan(now, target, comparators["probabilities"]),
                            "return_challenger": returns.predict(history, features, symbol=symbol, now=now),
                            "return_evaluation_cohort": return_cohort.plan(now, target),
                        },
                    ))
                    reason = "prediction_recorded"
            elif eligible:
                reason = "already_recorded"
        results.append({"symbol": symbol, "status": reason, "scored": scored, "expired": expired, "invalid": invalid})
    db.flush()
    return {"research_only": True, "execution_eligible": False, "results": results}


def online_research_status(db: Session):
    symbols = []
    now = datetime.now(UTC)
    for symbol in sorted(ALLOWED_SYMBOLS):
        query = _forecasts(db, symbol)
        rows = query.filter_by(status="scored").order_by(OnlineResearchForecast.id.desc()).limit(WINDOW).all()
        valid_rows = [row for row in rows if _known_outcome(row, now)]
        invalid_scored = len(rows) - len(valid_rows)
        rows = valid_rows
        latest = query.order_by(OnlineResearchForecast.id.desc()).first()
        brier = sum(row.outcome["model"]["brier"] for row in rows)/len(rows) if rows else None
        comparisons = []
        paired_rows = []
        for row in rows:
            expected = _comparison_scores(row.prediction, row.outcome["label"])
            if expected is not None and row.outcome.get("comparators") == expected:
                comparisons.append(expected["scores"])
                paired_rows.append(row)
        symbols.append({
            "symbol": symbol, "scored": query.filter_by(status="scored").count(),
            "pending": query.filter_by(status="pending").count(), "expired": query.filter_by(status="expired").count(),
            "invalid_forecasts": query.filter_by(status="invalid").count(),
            "metric_window": len(rows),
            "invalid_scored_observations": invalid_scored,
            "brier": brier,
            "neutral_brier": 0.25 if rows else None,
            "beats_neutral_baseline": brier < 0.25 if brier is not None else None,
            "baseline_brier": sum(row.outcome["baseline"]["brier"] for row in rows)/len(rows) if rows else None,
            "latest_prediction_at": _aware_utc(latest.issued_at).isoformat() if latest else None,
            "shadow_returns": shadow.summarize(paired_rows),
            "return_challenger": {**returns.summarize(rows, symbol=symbol, now=datetime.now(UTC)),
                "latest_prediction": latest.prediction.get("return_challenger") if latest and _valid_prediction(latest.prediction) else None,
                "nonoverlapping": return_cohort.report(
                    query.order_by(OnlineResearchForecast.id.desc()).limit(WINDOW).all(),
                    symbol=symbol, now=datetime.now(UTC))},
            "strategy_comparison": {
                "version": COMPARISON_VERSION, "paired_observations": len(comparisons),
                "execution_eligible": False, "profitability_proven": False,
                "strategies": [{"name": name,
                    "brier": sum(scores[name]["brier"] for scores in comparisons)/len(comparisons) if comparisons else None,
                    "log_loss": sum(scores[name]["log_loss"] for scores in comparisons)/len(comparisons) if comparisons else None,
                } for name in COMPARATORS],
            },
        })
    return {"version": VERSION, "research_only": True, "execution_eligible": False, "symbols": symbols}
