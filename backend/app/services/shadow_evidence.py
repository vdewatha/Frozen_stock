"""Read-only version-bound prediction evidence; never paper P&L or promotion."""
from datetime import datetime, timedelta, timezone
import hashlib
import math

from sqlalchemy import select

from app.models.crypto_data import CryptoCandle
from app.models.models import ResearchModelRun
from app.models.shadow import ShadowDecision, ShadowModelBinding
from app.services.crypto_collection import _check
from app.services.qualification import QualificationPolicy
from app.services.shadow_pipeline import INSTRUMENT, canonical, stamp, validate_spec


def _utc(value):
    # These SQL DateTime columns deliberately store naive UTC.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def shadow_evidence_report(db, binding_id, *, as_of=None, policy=QualificationPolicy()):
    """Compute a report without modifying rows or fabricating observation time.

    ``as_of`` is an internal deterministic-test seam; the HTTP endpoint always
    supplies the real clock. Invalid records are counted, excluded and reported.
    """
    with db.no_autoflush:
        return _report(db, binding_id, as_of=as_of, policy=policy)


def _report(db, binding_id, *, as_of, policy):
    clock = stamp(as_of or datetime.now(timezone.utc))
    binding = db.get(ShadowModelBinding, binding_id)
    if binding is None:
        raise ValueError("Unknown shadow binding")
    run = db.scalar(select(ResearchModelRun).where(ResearchModelRun.run_id == binding.run_id))
    integrity = True
    try:
        if (run is None or validate_spec(run, binding.spec) != binding.spec_sha256
            or run.manifest_sha256 != binding.manifest_sha256 or binding.instrument != INSTRUMENT
            or binding.timeframe_minutes != 60):
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        integrity = False
    decisions = list(db.scalars(select(ShadowDecision).where(ShadowDecision.binding_id == binding_id)
        .order_by(ShadowDecision.observed_at, ShadowDecision.id)))
    candles = list(db.scalars(select(CryptoCandle).where(CryptoCandle.instrument_id == INSTRUMENT,
        CryptoCandle.timeframe == "1h").order_by(CryptoCandle.opened_at)))
    counts = {"records": len(decisions), "observed": 0, "scored": 0,
        "pending": 0, "matured_unscored": 0, "excluded_backfilled": 0,
        "excluded_invalid": 0, "excluded_future": 0, "invalid_outcomes": 0}
    accepted, scores = [], []
    for decision in decisions:
        observed, bar_close = _utc(decision.observed_at), _utc(decision.bar_close)
        if observed > clock:
            counts["excluded_future"] += 1
            continue
        if decision.backfilled:
            counts["excluded_backfilled"] += 1
            continue
        try:
            if (not integrity or decision.spec_sha256 != binding.spec_sha256
                or decision.eligible_for_qualification or not _number(decision.probability)
                or not 0 <= decision.probability <= 1
                or not _number(decision.reference_price) or decision.reference_price <= 0
                or not _number(decision.latency_seconds)
                or decision.latency_seconds != (observed - bar_close).total_seconds()
                or not 0 <= decision.latency_seconds <= 300
                or _utc(binding.created_at) > observed or _utc(run.created_at) > _utc(binding.created_at)
                or stamp(binding.spec["training_cutoff"]) >= bar_close):
                raise ValueError()
            available = [c for c in candles if stamp(c.observed_at) <= observed
                and stamp(c.opened_at) + timedelta(hours=1) <= observed]
            _check(available, observed, 51)
            if (stamp(available[-1].opened_at) + timedelta(hours=1) != bar_close
                or float(available[-1].close) != decision.reference_price
                or hashlib.sha256(canonical([c.content_sha256 for c in available])).hexdigest() != decision.data_sha256):
                raise ValueError()
        except (ValueError, TypeError, KeyError, OverflowError):
            counts["excluded_invalid"] += 1
            continue
        accepted.append(decision)
        counts["observed"] += 1
        target_time = bar_close + timedelta(hours=binding.spec["horizon_bars"])
        outcome = decision.outcome
        if outcome is None:
            counts["pending"] += 1
            if target_time <= clock:
                counts["matured_unscored"] += 1
            continue
        try:
            if not isinstance(outcome, dict):
                raise ValueError()
            scored_at = stamp(outcome["scored_at"])
            if scored_at > clock:
                counts["pending"] += 1
                if target_time <= clock:
                    counts["matured_unscored"] += 1
                continue
            target_rows = [c for c in candles if stamp(c.observed_at) <= scored_at
                and stamp(c.opened_at) + timedelta(hours=1) <= scored_at]
            _check(target_rows, scored_at, 1)
            target = next((c for c in target_rows if stamp(c.opened_at) + timedelta(hours=1) == target_time), None)
            if (target is None or scored_at < target_time or scored_at < observed
                or stamp(outcome["target_bar_close"]) != target_time
                or outcome.get("eligible_for_qualification") is not False
                or outcome.get("target_data_sha256") != target.content_sha256):
                raise ValueError()
            realized = float(target.close) / decision.reference_price - 1
            label = int(realized > 0)
            brier = (decision.probability - label) ** 2
            if (not _number(outcome.get("realized_return"))
                or not math.isclose(outcome["realized_return"], realized, rel_tol=1e-12, abs_tol=1e-12)
                or type(outcome.get("up")) is not bool or outcome["up"] != bool(label)
                or not _number(outcome.get("brier_score"))
                or not math.isclose(outcome["brier_score"], brier, rel_tol=1e-12, abs_tol=1e-12)):
                raise ValueError()
            scores.append((decision.probability, label, brier))
            counts["scored"] += 1
        except (ValueError, KeyError, TypeError, OverflowError):
            counts["invalid_outcomes"] += 1
    first = _utc(accepted[0].observed_at) if accepted else None
    last = _utc(accepted[-1].observed_at) if accepted else None
    bars = sorted({_utc(d.bar_close) for d in accepted})
    expected = int((bars[-1] - bars[0]).total_seconds() // 3600) + 1 if bars else 0
    span = (last - first).total_seconds() if first is not None else 0
    metrics = None
    if scores:
        clipped = [(min(max(p, 1e-15), 1 - 1e-15), y) for p, y, _ in scores]
        metrics = {"brier_score": sum(b for _, _, b in scores) / len(scores),
            "log_loss": -sum(y * math.log(p) + (1-y) * math.log(1-p) for p, y in clipped) / len(scores),
            "directional_accuracy_at_0_5": sum(int(p >= .5) == y for p, y, _ in scores) / len(scores),
            "scored_observations": len(scores), "returns_are_price_labels_not_pnl": True}
    return {"report_version": "shadow-evidence-v1", "evaluated_at": clock.isoformat(),
        "binding_id": binding.id, "model_run_id": binding.run_id, "spec_sha256": binding.spec_sha256,
        "manifest_sha256": binding.manifest_sha256, "instrument": binding.instrument,
        "binding_integrity_passed": integrity, "counts": counts,
        "observation_window": {"first_observed_at": first.isoformat() if first else None,
            "last_observed_at": last.isoformat() if last else None,
            "observed_span_seconds": span, "observed_span_days": span / 86400,
            "distinct_observation_dates": len({_utc(d.observed_at).date() for d in accepted}),
            "expected_hourly_observations_within_span": expected,
            "missing_hourly_observations_within_span": expected - len(bars),
            "coverage_fraction_within_span": len(bars) / expected if expected else None},
        "prediction_metrics": metrics, "eligible_for_qualification": False, "live_authorized": False,
        "status": "research_only" if integrity else "blocked_invalid_binding",
        "qualification_policy": {"version": policy.version, "sha256": policy.fingerprint,
            "required_completed_paper_trades": policy.minimum_trades,
            "required_observed_paper_days": policy.minimum_days},
        "missing_requirements": ["qualifying_observed_paper_ledger", "net_trade_profit_and_cost_evidence",
            "reconciled_execution_and_equity_drawdown", "benchmark_and_regime_evidence",
            "independent_leakage_and_feature_parity_review", "separate_human_live_capital_approval"],
        "limitations": ["Shadow predictions are not completed trades or paper profits.",
            "Observation span uses first and last recorded observations, never time since installation.",
            "Sparse observation windows do not prove continuous operation.",
            "Overlapping forecast horizons are correlated; metrics are descriptive, not significance tests.",
            "This report never promotes models or grants trading permission."]}
