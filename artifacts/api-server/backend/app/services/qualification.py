"""Pure, deterministic qualification decision; never authorizes an order.

Only an ingestion/ledger service may assemble evidence. This module does not
authenticate an arbitrary dictionary or infer missing observations.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import math


@dataclass(frozen=True)
class QualificationPolicy:
    version: str = "paper-pilot-v1"
    minimum_trades: int = 100
    minimum_days: int = 84
    minimum_profit_factor: float = 1.2
    maximum_drawdown: float = 0.1
    maximum_brier_score: float = 0.25
    minimum_regimes: int = 2

    def __post_init__(self):
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("Policy version must be a nonblank string")
        if any(type(value) is not int for value in (self.minimum_trades, self.minimum_days, self.minimum_regimes)):
            raise ValueError("Policy counts must be finite integers")
        if self.minimum_trades < 100 or self.minimum_days < 84 or self.minimum_regimes < 2:
            raise ValueError("Policy cannot weaken the pilot qualification floor")
        for value in (self.minimum_profit_factor, self.maximum_drawdown, self.maximum_brier_score):
            if not math.isfinite(value):
                raise ValueError("Policy thresholds must be finite")
        if self.minimum_profit_factor < 1.2 or not 0 < self.maximum_drawdown <= .1 or not 0 < self.maximum_brier_score <= .25:
            raise ValueError("Invalid qualification thresholds")

    @property
    def fingerprint(self):
        return sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


def _number(evidence, name):
    value = evidence.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return value


def _timestamp(evidence, name):
    value = evidence.get(name)
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except ValueError:
        return None


def evaluate_qualification(evidence: dict, policy: QualificationPolicy = QualificationPolicy(), *, evaluated_at: datetime) -> dict:
    if not isinstance(evaluated_at, datetime) or evaluated_at.tzinfo is None:
        raise ValueError("Evaluation requires an aware trusted clock timestamp")
    checks = {}
    def check(name, passes):
        checks[name] = bool(passes)

    for name in ("model_id", "model_artifact_sha256", "dataset_sha256", "ledger_snapshot_sha256"):
        value = evidence.get(name)
        valid = isinstance(value, str) and bool(value.strip())
        if name.endswith("sha256"):
            valid = valid and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
        check(name, valid)
    check("observed_paper_only", evidence.get("mode") == "paper" and evidence.get("evidence_origin") == "observed_ledger")
    for name in ("leakage_checks_passed", "feature_parity_passed", "costs_included", "reconciliation_passed", "trusted_data_only"):
        check(name, evidence.get(name) is True)
    for name in ("critical_incidents", "manual_trades", "synthetic_trades", "backfilled_decisions"):
        check(name, _number(evidence, name) == 0)
    count = _number(evidence, "completed_trades")
    check("minimum_trades", count is not None and count == int(count) and count >= policy.minimum_trades)
    profit_factor = _number(evidence, "profit_factor")
    expectancy = _number(evidence, "net_expectancy")
    drawdown = _number(evidence, "max_drawdown")
    brier = _number(evidence, "brier_score")
    sharpe = _number(evidence, "sharpe_ratio")
    benchmark = _number(evidence, "benchmark_sharpe_ratio")
    check("profit_factor", profit_factor is not None and profit_factor >= policy.minimum_profit_factor)
    check("expectancy", expectancy is not None and expectancy > 0)
    check("drawdown", drawdown is not None and 0 <= drawdown <= policy.maximum_drawdown)
    check("calibration", brier is not None and 0 <= brier <= policy.maximum_brier_score)
    check("benchmark", sharpe is not None and benchmark is not None and sharpe > benchmark)
    regimes = evidence.get("regimes")
    check("regimes", isinstance(regimes, list) and all(isinstance(r, str) and r.strip() for r in regimes) and len({r.strip().casefold() for r in regimes}) >= policy.minimum_regimes)
    times = {name: _timestamp(evidence, name) for name in ("training_cutoff", "test_start", "test_end", "paper_start", "paper_end", "observed_at", "last_eligible_bar")}
    chronology = all(t is not None for t in times.values())
    if chronology:
        chronology = times["training_cutoff"] < times["test_start"] <= times["test_end"] < times["paper_start"] <= times["last_eligible_bar"] <= times["paper_end"] <= times["observed_at"] <= evaluated_at
    check("chronology", chronology)
    check("minimum_duration", chronology and (times["paper_end"] - times["paper_start"]).total_seconds() >= policy.minimum_days * 86400)
    passed = all(checks.values())
    return {"status": "live_pilot_candidate" if passed else "blocked", "live_authorized": False,
            "requires_human_approval": True, "policy_version": policy.version, "policy_sha256": policy.fingerprint,
            "checks": checks, "failed_checks": [name for name, ok in checks.items() if not ok]}
