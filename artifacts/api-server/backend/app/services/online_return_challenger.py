"""Prospective cost-aware return experiment; no execution authority."""
from datetime import datetime, timedelta
from hashlib import sha256
import json
from math import isfinite
from statistics import mean

from sklearn.linear_model import Ridge

from app.services import online_shadow_returns as shadow
from app.services.intraday_data import _aware_utc

VERSION = "iex-ridge-return-v1"
MIN_TRAINING = 50
COST = 5
# Costs apply to both notionals, including the predicted change in exit value.
HURDLE = 2 * COST / (1 - COST / 10000)


def _features(values):
    if len(values) != 3 or any(not isfinite(v) or not -5 <= v <= 5 for v in values):
        raise ValueError("Invalid return features")
    return list(values)


def _valid_row(row, now, symbol):
    try:
        observed = datetime.fromisoformat(row.outcome["observed_at"])
        return (row.symbol == symbol and row.version == "iex-sgd-v1" and row.status == "scored"
                and observed.tzinfo is not None
                and _aware_utc(row.issued_at) < _aware_utc(row.target_at)
                and _aware_utc(row.target_at) + timedelta(minutes=1) <= observed <= now
                and shadow.summarize([row])["paired_observations"] == 1)
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def predict(rows, features, *, symbol, now):
    if now.tzinfo is None:
        raise ValueError("Aware prediction time required")
    features = _features(features)
    samples = []
    for row in sorted(rows, key=lambda r: (_aware_utc(r.issued_at), r.id)):
        if not _valid_row(row, now, symbol):
            continue
        try:
            x = _features(row.prediction["features"])
            observation = row.outcome["shadow"]
            target = (observation["exit_close"] / observation["entry_open"] - 1) * 10000
            samples.append({"id": row.id, "features": x, "return_bps": target,
                            "observed_at": row.outcome["observed_at"]})
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    estimate = 0.0
    if len(samples) >= MIN_TRAINING:
        model = Ridge(alpha=10.0, solver="svd")
        model.fit([s["features"] for s in samples],
                  [max(-500., min(500., s["return_bps"])) for s in samples])
        estimate = float(model.predict([features])[0])
    if not isfinite(estimate):
        raise ValueError("Nonfinite return prediction")
    return {"version": VERSION, "symbol": symbol, "issued_at": now.isoformat(),
            "model": "ridge", "alpha": 10.0, "training_target_clip_bps": 500,
            "minimum_training": MIN_TRAINING, "training_examples": len(samples),
            "training_sha256": sha256(json.dumps(samples, sort_keys=True, allow_nan=False).encode()).hexdigest(),
            "predicted_gross_bps": estimate, "cost_bps_per_side": COST,
            "long_hurdle_bps": HURDLE,
            "action": "long" if len(samples) >= MIN_TRAINING and estimate > HURDLE else "cash"}


def summarize(rows, *, symbol, now):
    pairs, invalid, unavailable, declared = [], 0, 0, 0
    for row in rows:
        declaration = row.prediction.get("return_challenger")
        if declaration is None:
            continue
        declared += 1
        try:
            estimate = declaration["predicted_gross_bps"]
            count = declaration["training_examples"]
            digest = declaration["training_sha256"]
            if (declaration["version"] != VERSION or declaration["symbol"] != symbol
                    or declaration["issued_at"] != _aware_utc(row.issued_at).isoformat()
                    or declaration["model"] != "ridge" or declaration["alpha"] != 10.0
                    or declaration["training_target_clip_bps"] != 500
                    or declaration["minimum_training"] != MIN_TRAINING
                    or type(count) is not int or count < 0
                    or not isinstance(digest, str) or len(digest) != 64
                    or any(c not in "0123456789abcdef" for c in digest)
                    or not isfinite(estimate) or (count < MIN_TRAINING and estimate != 0)
                    or declaration["cost_bps_per_side"] != COST
                    or declaration["long_hurdle_bps"] != HURDLE
                    or declaration["action"] != ("long" if count >= MIN_TRAINING and estimate > HURDLE else "cash")):
                raise ValueError("Invalid challenger declaration")
            if not _valid_row(row, now, symbol):
                if row.outcome.get("shadow", {}).get("status") == "unavailable":
                    unavailable += 1
                else:
                    invalid += 1
                continue
            observation = row.outcome["shadow"]
            scores = shadow.scores({"actions": {"challenger": int(declaration["action"] == "long"),
                                                "always_long": 1}},
                                   observation["entry_open"], observation["exit_close"])
            if not isfinite(estimate - scores["always_long"]["gross_bps"]):
                raise ValueError("Nonfinite return error")
            pairs.append((estimate, scores))
        except (KeyError, TypeError, ValueError, OverflowError):
            invalid += 1
    return {"version": VERSION, "declared_scored_forecasts": declared,
            "paired_observations": len(pairs), "invalid_observations": invalid,
            "unavailable_observations": unavailable,
            "cost_bps_per_side": COST, "long_hurdle_bps": HURDLE,
            "research_only": True, "execution_eligible": False, "profitability_proven": False,
            "overlapping_windows": True, "aggregation": "mean_per_forecast_not_portfolio_pnl",
            "mae_bps": mean(abs(p - s["always_long"]["gross_bps"]) for p, s in pairs) if pairs else None,
            "zero_baseline_mae_bps": mean(abs(s["always_long"]["gross_bps"]) for _, s in pairs) if pairs else None,
            "strategies": [{"name": name,
                "long_observations": sum(s[name]["action"] == "long" for _, s in pairs),
                "mean_net_bps": mean(s[name]["net_bps_by_cost"][str(COST)] for _, s in pairs),
            } for name in ("challenger", "always_long") ] if pairs else []}
