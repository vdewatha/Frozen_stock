"""Safe numeric inference on recorded data. Never loads pickle or places orders.

Caller owns commits, including blocked run audits. All observations are research
only: an operator's shadow binding is not model or capital approval.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.models import ResearchModelRun
from app.models.shadow import ShadowDecision, ShadowModelBinding, ShadowRunAudit
from app.services.crypto_collection import CryptoDataError, load_closed_history
from app.services.feature_pipeline import DEFAULT_FEATURES, feature_config_id, generate_features

INSTRUMENT = "crypto_spot:KRAKEN:BTC:USD"
NAMES = [f.name for f in DEFAULT_FEATURES]


def insert_once(db, row, lookup):
    connection = db.connection()
    if connection.dialect.name == "sqlite" and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql("BEGIN")
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
        return row
    except IntegrityError:
        winner = lookup()
        if winner is None:
            raise
        return winner


def canonical(value):
    return json.dumps(value, sort_keys=True, indent=2, allow_nan=False).encode()


def stamp(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("UTC-aware timestamp required")
    return value.astimezone(timezone.utc)


def validate_spec(run, spec):
    required = {"version", "run_id", "feature_config_id", "feature_names", "mean", "scale", "coef", "intercept",
                "training_cutoff", "instrument", "timeframe_minutes", "horizon_bars"}
    if not isinstance(spec, dict) or set(spec) != required:
        raise ValueError("Unsupported portable model schema")
    if (type(spec["version"]) is not int or spec["version"] != 1 or spec["run_id"] != run.run_id
            or spec["feature_config_id"] != feature_config_id() or spec["feature_names"] != NAMES
            or spec["instrument"] != INSTRUMENT or type(spec["timeframe_minutes"]) is not int or spec["timeframe_minutes"] != 60):
        raise ValueError("Model identity or feature mismatch")
    metadata = run.training_metadata
    if (metadata.get("instrument_id") != spec["instrument"] or metadata.get("timeframe_minutes") != 60
            or metadata.get("feature_config_id") != spec["feature_config_id"]
            or type(spec["horizon_bars"]) is not int or spec["horizon_bars"] not in (1, 5, 20)
            or metadata.get("horizon_bars") != spec["horizon_bars"]
            or stamp(spec["training_cutoff"]) != stamp(metadata["train_label_end"])):
        raise ValueError("Training binding mismatch")
    for name in ("mean", "scale", "coef"):
        values = spec[name]
        if (not isinstance(values, list) or len(values) != len(NAMES)
                or any(type(v) not in (float, int) or not math.isfinite(v) or abs(v) > 1e12 for v in values)
                or (name == "scale" and any(v <= 0 for v in values))):
            raise ValueError("Invalid numeric model vector")
    if type(spec["intercept"]) not in (int, float) or not math.isfinite(spec["intercept"]) or abs(spec["intercept"]) > 1e12:
        raise ValueError("Invalid intercept")
    digest = hashlib.sha256(canonical(spec)).hexdigest()
    if (run.manifest_sha256 != hashlib.sha256(canonical(metadata)).hexdigest()
            or metadata.get("files", {}).get("logistic_regression.json") != digest
            or run.status != "experimental" or run.eligible_for_trading):
        raise ValueError("Registry integrity failure")
    return digest


def register_shadow_model(db, run_id, spec, actor):
    if not isinstance(actor, str) or not actor.strip() or len(actor) > 120:
        raise ValueError("Named shadow operator required")
    run = db.scalar(select(ResearchModelRun).where(ResearchModelRun.run_id == run_id))
    if run is None:
        raise ValueError("Unregistered research run")
    digest = validate_spec(run, spec)
    if stamp(spec["training_cutoff"]) >= datetime.now(timezone.utc):
        raise ValueError("Training cutoff is not in the past")
    old = db.scalar(select(ShadowModelBinding).where(ShadowModelBinding.spec_sha256 == digest))
    if old:
        if old.spec != spec or old.manifest_sha256 != run.manifest_sha256:
            raise ValueError("Binding integrity failure")
        return old
    binding = ShadowModelBinding(run_id=run_id, spec_sha256=digest, manifest_sha256=run.manifest_sha256,
                                 instrument=spec["instrument"], timeframe_minutes=60,
                                 spec=json.loads(canonical(spec)), actor=actor.strip())
    winner = insert_once(db, binding, lambda: db.scalar(select(ShadowModelBinding).where(ShadowModelBinding.spec_sha256 == digest)))
    if winner.spec != spec or winner.manifest_sha256 != run.manifest_sha256:
        raise ValueError("Binding integrity failure")
    return winner


def run_shadow(db, binding_id, as_of=None):
    clock = stamp(as_of or datetime.now(timezone.utc))
    binding = db.get(ShadowModelBinding, binding_id)
    if binding is None:
        raise ValueError("Unknown shadow binding")
    try:
        run = db.scalar(select(ResearchModelRun).where(ResearchModelRun.run_id == binding.run_id))
        if (run is None or validate_spec(run, binding.spec) != binding.spec_sha256 or binding.manifest_sha256 != run.manifest_sha256
                or binding.instrument != binding.spec["instrument"] or binding.timeframe_minutes != binding.spec["timeframe_minutes"]):
            raise ValueError("Binding integrity failure")
        rows = load_closed_history(db, as_of=clock, minimum=51)
        bar_close = stamp(rows[-1].opened_at) + timedelta(hours=1)
        if stamp(binding.spec["training_cutoff"]) >= bar_close:
            raise ValueError("Prediction overlaps training")
        existing = db.scalar(select(ShadowDecision).where(ShadowDecision.binding_id == binding_id,
                              ShadowDecision.bar_close == bar_close.replace(tzinfo=None)))
        if existing:
            return existing
        frame = pd.DataFrame([dict(date=stamp(r.opened_at), close=float(r.close), volume=float(r.volume)) for r in rows])
        features = generate_features(frame)[NAMES].iloc[-1].to_numpy(dtype=float)
        if not np.isfinite(features).all():
            raise ValueError("Insufficient finite features")
        spec = binding.spec
        with np.errstate(over="raise", invalid="raise", divide="raise"):
            score = float(np.sum(((features - np.array(spec["mean"])) / np.array(spec["scale"])) * np.array(spec["coef"]))) + spec["intercept"]
        if not math.isfinite(score):
            raise ValueError("Nonfinite model score")
        probability = 1 / (1 + math.exp(-score)) if score >= 0 else math.exp(score) / (1 + math.exp(score))
        latency = (clock - bar_close).total_seconds()
        decision = ShadowDecision(binding_id=binding_id, bar_close=bar_close.replace(tzinfo=None),
             observed_at=clock.replace(tzinfo=None), data_sha256=hashlib.sha256(canonical([r.content_sha256 for r in rows])).hexdigest(),
             spec_sha256=binding.spec_sha256, probability=probability, reference_price=float(rows[-1].close),
             intended_action="buy" if probability >= .6 else "sell" if probability <= .4 else "hold",
             backfilled=latency > 300, eligible_for_qualification=False, latency_seconds=latency)
        winner = insert_once(db, decision, lambda: db.scalar(select(ShadowDecision).where(
            ShadowDecision.binding_id == binding_id, ShadowDecision.bar_close == bar_close.replace(tzinfo=None))))
        if winner is not decision:
            return winner
        db.add(ShadowRunAudit(binding_id=binding_id, observed_at=clock.replace(tzinfo=None), status="observed", reason="research_only"))
        return decision
    except (ValueError, KeyError, TypeError, FloatingPointError, OverflowError):
        db.add(ShadowRunAudit(binding_id=binding_id, observed_at=clock.replace(tzinfo=None), status="blocked", reason="invalid_model_or_market_data"))
        db.flush()
        return None


def score_shadow(db, as_of=None):
    clock = stamp(as_of or datetime.now(timezone.utc))
    try:
        rows = load_closed_history(db, as_of=clock, minimum=1)
    except CryptoDataError:
        return 0
    by_close = {stamp(r.opened_at) + timedelta(hours=1): r for r in rows}
    count = 0
    for decision in db.scalars(select(ShadowDecision).where(ShadowDecision.outcome.is_(None))):
        binding = db.get(ShadowModelBinding, decision.binding_id)
        try:
            run = db.scalar(select(ResearchModelRun).where(ResearchModelRun.run_id == binding.run_id))
            if (run is None or validate_spec(run, binding.spec) != binding.spec_sha256
                    or binding.manifest_sha256 != run.manifest_sha256 or decision.spec_sha256 != binding.spec_sha256
                    or binding.instrument != binding.spec["instrument"] or binding.timeframe_minutes != binding.spec["timeframe_minutes"]):
                raise ValueError("Binding integrity failure")
        except (ValueError, KeyError, TypeError, AttributeError):
            db.add(ShadowRunAudit(binding_id=decision.binding_id, observed_at=clock.replace(tzinfo=None),
                                 status="blocked", reason="scoring_integrity_failure"))
            continue
        target = decision.bar_close.replace(tzinfo=timezone.utc) + timedelta(hours=binding.spec["horizon_bars"])
        if target not in by_close:
            continue
        end = float(by_close[target].close)
        change = end / decision.reference_price - 1
        decision.outcome = {"scored_at": clock.isoformat(), "target_bar_close": target.isoformat(),
                            "realized_return": change, "up": change > 0,
                            "brier_score": (decision.probability - int(change > 0)) ** 2,
                            "target_data_sha256": by_close[target].content_sha256,
                            "eligible_for_qualification": False}
        count += 1
    db.flush()
    return count
