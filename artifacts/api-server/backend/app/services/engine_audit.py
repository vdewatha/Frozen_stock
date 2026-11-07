"""Engine audit: expected vs realised per adopted change, the engine's hit rate, and a calibration factor.

The experiment engine (``experiment_engine.py``) now adopts changes on its
own: a repeating loss lesson becomes a replay, ``decide`` applies the
pre-registered rule, the winner goes on the strategy row and a forward
verdict confirms or reverts it.  The next risk is that it overfits itself:
every replay gain is an in-sample claim, and the engine tries several
candidate values per experiment.  This module keeps it honest.

1. **Expected vs realised per change** (``change_record``).  Each
   ``change_adopted`` row carries the replay rows of the baseline and the
   adopted setup per window (``expected``) and the last trades before the
   change (``recent_before``); each ``change_verdict`` row carries the
   realised forward record (``after``) and the alternatives' shadow record
   (``shadow``, from ``setup_shadow.py``).  The expected gain is adopted
   minus baseline expectancy over the 2020+ window (the window closest to
   the regime the change trades in); the realised gain is after minus
   before over the live trades, or, while those are too few, the adopted
   value against the value it replaced over the same shadow decisions.  The
   shortfall is expected minus realised; the ratio realised over expected is
   the engine's honesty on that change.
2. **Hit rate** (``hit_rate``).  Across all ``experiment`` rows: how many
   were adopted, how the verdicts fell (``working`` / ``not_helping`` /
   ``inconclusive``), the median realised-over-expected ratio with a
   percentile-bootstrap confidence interval over the judged changes (at
   least ``experiment_verdict_min_trades`` forward trades), and a
   multiple-testing view: candidate setups tried per adoption and the share
   of adoptions luck alone would produce at that count.  The luck share is a
   Bonferroni-style expected false-positive count (``ALPHA`` per candidate
   test) over the adoptions, not the deflated Sharpe ratio: the replay rows
   store expectancies and their CI, not per-period Sharpe ratios, so the DSR
   helper in ``backtest_statistics.py`` has no input here.
3. **Automatic tightening** (``next_factor``).  When the median ratio over at
   least ``MIN_JUDGED`` judged changes is below ``TIGHTEN_BELOW``, the audit
   writes an ``engine_audit`` row that sets a calibration factor in
   ``[FACTOR_FLOOR, 1.0]``.  ``decide`` multiplies the factor into each
   candidate's expected gain and requires the deflated gain to cover the
   optimism's share of the engine's own revert margin, so a candidate must
   beat the baseline by more when the engine has been over-optimistic.  The
   factor returns to 1.0 once the ratio recovers above ``RECOVER_ABOVE``;
   between the two thresholds it follows the median while tightened
   (hysteresis).  The rule text and the structure of ``decide`` are
   unchanged; at factor 1.0 the rule is exactly the pre-registered one.
4. **Surface.**  ``GET /stock-paper/engine-audit``, the ``engine_audit``
   section of the weekly report and of the learning scoreboard, and a weekly
   job (Saturday, after the weekly report) that recomputes and stores the
   row.  Audit rows only; no migration.

Paper only.  Nothing here touches orders, exits or the strategy rows; it
only reads the engine's ledger and sets the margin the engine's rule asks of
the next candidate.
"""
from __future__ import annotations

from datetime import datetime, timezone
import logging
import math
from statistics import median
from typing import Any, Iterable, Mapping, Optional

import numpy as np
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import AuditLog
from app.services.audit import write_audit_log

logger = logging.getLogger(__name__)
UTC = timezone.utc
EVENT = "engine_audit"
ACTION = "engine_audit"
EXPECTED_WINDOW = "2020+"
FALLBACK_WINDOWS = ("2020+", "2015+", "all")
MIN_JUDGED = 5
TIGHTEN_BELOW = 0.5
RECOVER_ABOVE = 0.8
FACTOR_FLOOR = 0.5
ALPHA = 0.05
BOOTSTRAP_SAMPLES = 2000
BOOTSTRAP_SEED = 7
CONFIDENCE = 0.95
MULTIPLE_TESTING_METHOD = "bonferroni_expected_false_positives"
METHOD = ("Expected gain = adopted minus baseline replay expectancy (bps per trade) over the 2020+ window; realised "
          "gain = forward expectancy after the change minus the last trades before it (live trades), or the adopted "
          "value against the value it replaced over the same shadow decisions while the live trades are too few. "
          f"Ratio = realised / expected. Median and {CONFIDENCE:.0%} percentile-bootstrap CI over changes with at "
          "least the verdict's min_trades forward trades. Luck share = candidates tried x "
          f"{ALPHA:g} / adoptions (Bonferroni-style expected false positives). Factor: median ratio over >= "
          f"{MIN_JUDGED} judged changes < {TIGHTEN_BELOW:g} tightens to max({FACTOR_FLOOR:g}, median); > "
          f"{RECOVER_ABOVE:g} returns to 1.0; in between the factor follows the median while tightened.")


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()
    return str(value)


def _f(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _r(value: Optional[float], digits: int = 2) -> Optional[float]:
    return None if value is None else round(float(value), digits)


# ----------------------------------------------------------- per change

def expected_gain(change: Mapping) -> dict:
    """Adopted minus baseline replay expectancy, from the ``expected`` block of a ``change_adopted`` payload."""
    expected = change.get("expected") or {}
    baseline, adopted = expected.get("baseline") or {}, expected.get("adopted") or {}
    for window in FALLBACK_WINDOWS:
        b, a = _f((baseline.get(window) or {}).get("expectancy_bps")), _f((adopted.get(window) or {}).get("expectancy_bps"))
        if b is not None and a is not None:
            return {"window": window, "baseline_bps": _r(b, 1), "adopted_bps": _r(a, 1), "gain_bps": _r(a - b, 2),
                    "replay_trades": int((adopted.get(window) or {}).get("trades") or 0),
                    "calibration_factor": _f(expected.get("calibration_factor")) or 1.0}
    return {"window": None, "baseline_bps": None, "adopted_bps": None, "gain_bps": None, "replay_trades": 0,
            "calibration_factor": _f(expected.get("calibration_factor")) or 1.0}


def _before_label(change: Mapping) -> Optional[str]:
    """The shadow label of the value the change replaced (gate templates only)."""
    from app.services.experiment_engine import TEMPLATE_BY_NAME
    from app.services.setup_shadow import candidate_label, live_candidate

    template = TEMPLATE_BY_NAME.get(change.get("template") or "")
    if template is None or template.kind != "gate":
        return None
    try:
        return candidate_label(template, live_candidate(template, change.get("before") or {}))
    except Exception:
        return None


def shadow_gain(shadow: Optional[Mapping], before_label: Optional[str]) -> Optional[dict]:
    """The adopted value against the value it replaced over the same shadow decisions (``setup_shadow`` record)."""
    if not shadow or not before_label:
        return None
    candidates = shadow.get("candidates") or []
    live = next((c for c in candidates if c.get("is_live")), None)
    previous = next((c for c in candidates if c.get("candidate") == before_label and not c.get("is_live")), None)
    if live is None or previous is None:
        return None
    delta = _f(previous.get("delta_expectancy_bps"))  # previous minus adopted
    overlap = int(previous.get("overlap") or 0)
    if delta is None or overlap <= 0:
        return None
    return {"gain_bps": _r(-delta, 2), "overlap": overlap, "adopted_bps": _f(live.get("expectancy_bps")),
            "previous_bps": _f(previous.get("expectancy_bps")), "adopted_trades": int(live.get("trades") or 0),
            "previous": before_label}


def realised_gain(change: Mapping, verdict: Optional[Mapping], shadow: Optional[Mapping] = None) -> dict:
    """After minus before on the live trades; the shadow delta while those are too few; None without either."""
    before = _f((change.get("recent_before") or {}).get("expectancy_bps"))
    after_block = (verdict or {}).get("after") or {}
    after, trades = _f(after_block.get("expectancy_bps")), int(after_block.get("trades") or 0)
    min_trades = int((change.get("watch") or {}).get("min_trades") or settings.experiment_verdict_min_trades)
    shadow_block = shadow if shadow is not None else (verdict or {}).get("shadow")
    from_shadow = shadow_gain(shadow_block, _before_label(change))
    if after is not None and before is not None and (trades >= min_trades or from_shadow is None):
        return {"source": "live", "gain_bps": _r(after - before, 2), "before_bps": _r(before, 1),
                "after_bps": _r(after, 1), "forward_trades": trades, "shadow": from_shadow}
    if from_shadow is not None:
        return {"source": "shadow", "gain_bps": from_shadow["gain_bps"], "before_bps": _r(from_shadow["previous_bps"], 1),
                "after_bps": _r(from_shadow["adopted_bps"], 1), "forward_trades": from_shadow["overlap"],
                "shadow": from_shadow}
    return {"source": None, "gain_bps": None, "before_bps": _r(before, 1), "after_bps": _r(after, 1),
            "forward_trades": trades, "shadow": None}


def change_record(change: Mapping, verdict: Optional[Mapping] = None, shadow: Optional[Mapping] = None, *,
                  min_trades: Optional[int] = None) -> dict:
    """One row of the expected-vs-realised table for an adopted change."""
    min_trades = int(min_trades if min_trades is not None else
                     (change.get("watch") or {}).get("min_trades") or settings.experiment_verdict_min_trades)
    expected = expected_gain(change)
    realised = realised_gain(change, verdict, shadow)
    gain_e, gain_r = expected["gain_bps"], realised["gain_bps"]
    shortfall = _r(gain_e - gain_r, 2) if gain_e is not None and gain_r is not None else None
    ratio = _r(gain_r / gain_e, 4) if gain_e is not None and gain_r is not None and gain_e > 0 else None
    judged = ratio is not None and realised["forward_trades"] >= min_trades
    return {
        "change_id": change.get("change_id"), "strategy_type": change.get("strategy_type"),
        "template": change.get("template"), "description": change.get("description"),
        "adopted_at": change.get("adopted_at"), "state": change.get("state"),
        "verdict": (verdict or {}).get("verdict") or change.get("verdict"),
        "expected_window": expected["window"], "expected_baseline_bps": expected["baseline_bps"],
        "expected_adopted_bps": expected["adopted_bps"], "expected_gain_bps": gain_e,
        "replay_trades": expected["replay_trades"], "calibration_factor_at_adoption": expected["calibration_factor"],
        "before_bps": realised["before_bps"], "after_bps": realised["after_bps"], "realised_gain_bps": gain_r,
        "realised_source": realised["source"], "forward_trades": realised["forward_trades"], "min_trades": min_trades,
        "shortfall_bps": shortfall, "realised_over_expected": ratio, "judged": judged,
        "shadow": realised["shadow"],
        # Both forward judges (verdict_stats.py): the one that acted and the e-process one in shadow.
        "verdict_method": (verdict or {}).get("verdict_method") or change.get("verdict_method"),
        "verdict_fixed_20": (verdict or {}).get("verdict_fixed_20") or change.get("verdict_fixed_20"),
        "verdict_e_process": _e_verdict_label((verdict or {}).get("verdict_e_process")
                                              or change.get("verdict_e_process")),
    }


def _e_verdict_label(block: Optional[Mapping]) -> Optional[str]:
    if not isinstance(block, Mapping):
        return None
    return block.get("verdict") or block.get("status")


def judge_agreement(records: Iterable[Mapping]) -> dict:
    """How the fixed 20-trade rule and the e-process judge compare on the judged changes (both recorded)."""
    pairs = [(r.get("verdict_fixed_20"), r.get("verdict_e_process")) for r in records
             if r.get("verdict_fixed_20") and r.get("verdict_e_process")]
    acted = {"keep": ("working", "inconclusive"), "revert": ("not_helping",)}

    def action(verdict: Optional[str]) -> str:
        for name, verdicts in acted.items():
            if verdict in verdicts:
                return name
        return "watching"

    table: dict[str, int] = {}
    for fixed, eproc in pairs:
        key = f"{action(fixed)}/{action(eproc)}"
        table[key] = table.get(key, 0) + 1
    decided = [(action(a), action(b)) for a, b in pairs if action(a) != "watching" and action(b) != "watching"]
    agree = sum(1 for a, b in decided if a == b)
    return {"pairs": len(pairs), "fixed_20/e_process": table, "both_decided": len(decided),
            "agreement": _r(agree / len(decided), 4) if decided else None}


def verdict_fields(change: Mapping, after: Mapping, shadow: Optional[Mapping]) -> dict:
    """The additive expected-vs-realised fields a ``change_verdict`` payload carries (``review_changes``)."""
    record = change_record(change, {"after": after, "shadow": shadow})
    return {key: record[key] for key in ("expected_window", "expected_gain_bps", "realised_gain_bps",
                                         "realised_source", "shortfall_bps", "realised_over_expected",
                                         "calibration_factor_at_adoption")}


# ------------------------------------------------------------- hit rate

def bootstrap_median_ci(values: Iterable[float], *, samples: int = BOOTSTRAP_SAMPLES, confidence: float = CONFIDENCE,
                        seed: int = BOOTSTRAP_SEED, min_values: int = MIN_JUDGED) -> Optional[list]:
    """Percentile bootstrap interval for the median; None below ``min_values`` observations."""
    data = np.asarray([v for v in values if v is not None and math.isfinite(v)], dtype=float)
    if data.size < min_values:
        return None
    rng = np.random.default_rng(seed)
    draws = np.median(data[rng.integers(0, data.size, size=(samples, data.size))], axis=1)
    tail = (1 - confidence) / 2 * 100
    low, high = np.percentile(draws, [tail, 100 - tail])
    return [round(float(low), 4), round(float(high), 4)]


def multiple_testing(experiments: Iterable[Mapping]) -> dict:
    """Candidate setups tried per adoption and the share of adoptions luck alone would produce."""
    tried = adoptions = eligible = runs = 0
    for experiment in experiments:
        runs += 1
        setups = experiment.get("setups") or {}
        tried += max(len(setups) - 1, 0) if setups else len((experiment.get("rows") or {})) - 1
        if experiment.get("adopted"):
            eligible += 1
        if experiment.get("applied"):
            adoptions += 1
    expected_false = round(tried * ALPHA, 3)
    return {"method": MULTIPLE_TESTING_METHOD, "alpha": ALPHA, "experiments": runs, "candidates_tried": tried,
            "eligible": eligible, "adoptions": adoptions,
            "candidates_per_adoption": _r(tried / adoptions, 2) if adoptions else None,
            "expected_false_positives": expected_false,
            "luck_share_of_adoptions": _r(min(1.0, expected_false / adoptions), 4) if adoptions else None,
            "note": ("Each candidate test is one chance at a false positive at alpha; the expected count over "
                     "adoptions is the share of adoptions that would appear even if no candidate had an edge.")}


def hit_rate(records: Iterable[Mapping], experiments: Iterable[Mapping]) -> dict:
    """Adoptions, verdict counts, the median realised-over-expected ratio with its CI, and the luck share."""
    records = list(records)
    verdicts = {"working": 0, "not_helping": 0, "inconclusive": 0, "watching": 0}
    for record in records:
        verdicts[record.get("verdict") or "watching"] = verdicts.get(record.get("verdict") or "watching", 0) + 1
    judged = [r["realised_over_expected"] for r in records if r.get("judged")]
    ratios = [float(v) for v in judged if v is not None]
    med = _r(median(ratios), 4) if ratios else None
    shortfalls = [r["shortfall_bps"] for r in records if r.get("judged") and r.get("shortfall_bps") is not None]
    return {
        "changes": len(records), "judged": len(ratios), "verdicts": verdicts,
        "working_share": _r(verdicts["working"] / len(records), 4) if records else None,
        "realised_over_expected": {"median": med, "ci": bootstrap_median_ci(ratios), "confidence": CONFIDENCE,
                                   "n": len(ratios), "min_judged": MIN_JUDGED,
                                   "mean_shortfall_bps": _r(sum(shortfalls) / len(shortfalls), 2) if shortfalls else None},
        "multiple_testing": multiple_testing(experiments),
        "judges": judge_agreement(records),
    }


# ------------------------------------------------------------ calibration

def next_factor(current: float, ratio_median: Optional[float], judged: int) -> float:
    """The calibration factor the audit sets, with hysteresis between the two thresholds."""
    current = float(current if current is not None else 1.0)
    if ratio_median is None or judged < MIN_JUDGED:
        return current
    if ratio_median > RECOVER_ABOVE:
        return 1.0
    if ratio_median < TIGHTEN_BELOW:
        return round(max(FACTOR_FLOOR, min(1.0, ratio_median)), 4)
    if current < 1.0:  # tightened and not yet recovered: follow the median
        return round(max(FACTOR_FLOOR, min(1.0, ratio_median)), 4)
    return 1.0


def required_gain_bps(factor: float, margin_bps: float) -> float:
    """The raw replay gain a candidate needs so that ``factor x gain >= (1 - factor) x margin``."""
    factor = float(factor)
    if factor >= 1.0:
        return 0.0
    return round((1.0 - factor) * float(margin_bps) / factor, 2)


def _latest_row(db: Session) -> Optional[AuditLog]:
    return db.query(AuditLog).filter(AuditLog.event_type == EVENT, AuditLog.action == ACTION).order_by(
        AuditLog.id.desc()).first()


def latest_audit(db: Session) -> Optional[dict]:
    row = _latest_row(db)
    return dict(row.payload or {}) if row else None


def current_calibration(db: Session) -> dict:
    """The factor in force (1.0 without a row), with when and why it was set."""
    latest = latest_audit(db)
    block = (latest or {}).get("calibration") or {}
    factor = _f(block.get("factor"))
    if factor is None or not (FACTOR_FLOOR <= factor <= 1.0):
        factor = 1.0
    return {"factor": factor, "as_of": (latest or {}).get("as_of"), "status": block.get("status"),
            "reason": block.get("reason"), "ratio_median": block.get("ratio_median"), "judged": block.get("judged")}


def current_calibration_factor(db: Session) -> float:
    """The hook the engine reads before ``decide``: 1.0 without an audit row or when the ledger is unreadable."""
    try:
        return float(current_calibration(db)["factor"])
    except Exception as exc:
        db.rollback()
        logger.warning("engine audit: calibration factor unavailable, using 1.0: %s", exc)
        return 1.0


# ------------------------------------------------------------------ build

def _shadow_for(db: Session, change: Mapping, *, min_trades: int) -> Optional[dict]:
    """The live shadow record for a change still watching (its verdict, when any, carries a stored one)."""
    from app.services.experiment_engine import REVERT_EXPECTANCY_BPS, _parse
    from app.services.setup_shadow import shadow_track_record

    try:
        return shadow_track_record(db, change["strategy_type"], change["template"], since=_parse(change.get("adopted_at")),
                                   min_trades=min_trades, margin_bps=REVERT_EXPECTANCY_BPS)
    except Exception as exc:
        db.rollback()
        logger.warning("engine audit: shadow record unavailable for %s: %s", change.get("change_id"), exc)
        return None


def change_records(db: Session) -> list[dict]:
    """Expected vs realised for every adopted change (newest first)."""
    from app.services.experiment_engine import adopted_changes

    out = []
    for change in adopted_changes(db):
        verdict = (change.get("verdicts") or [None])[-1]
        min_trades = int((change.get("watch") or {}).get("min_trades") or settings.experiment_verdict_min_trades)
        shadow = None if verdict and verdict.get("shadow") else _shadow_for(db, change, min_trades=min_trades)
        out.append(change_record(change, verdict, shadow, min_trades=min_trades))
    return out


def build_engine_audit(db: Session, *, now: Optional[datetime] = None) -> dict:
    """The audit as of ``now``: table, hit rate, the factor in force and the one this audit would set."""
    from app.services.experiment_engine import REVERT_EXPECTANCY_BPS, RULE, experiment_history

    now = now or datetime.now(UTC)
    records = change_records(db)
    experiments = experiment_history(db)
    rate = hit_rate(records, experiments)
    in_force = current_calibration(db)
    ratio = rate["realised_over_expected"]
    proposed = next_factor(in_force["factor"], ratio["median"], ratio["n"])
    if ratio["n"] < MIN_JUDGED:
        status, reason = "waiting", (f"{ratio['n']} judged changes; the factor moves only from {MIN_JUDGED} "
                                     f"(each needs at least the verdict's min_trades forward trades)")
    elif proposed < in_force["factor"]:
        status, reason = "tightened", (f"median realised-over-expected {ratio['median']:.2f} over {ratio['n']} judged "
                                       f"changes is below {TIGHTEN_BELOW:g}")
    elif proposed > in_force["factor"]:
        status, reason = "relaxed", (f"median realised-over-expected {ratio['median']:.2f} over {ratio['n']} judged "
                                     f"changes is above {RECOVER_ABOVE:g}")
    else:
        status, reason = "unchanged", (f"median realised-over-expected {ratio['median']:.2f} over {ratio['n']} judged "
                                       f"changes keeps the factor at {in_force['factor']:g}")
    return {
        "as_of": _iso(now), "changes": records, "hit_rate": rate,
        "calibration": {"factor": proposed, "in_force": in_force["factor"], "in_force_as_of": in_force["as_of"],
                        "status": status, "reason": reason, "ratio_median": ratio["median"], "ratio_ci": ratio["ci"],
                        "judged": ratio["n"], "tighten_below": TIGHTEN_BELOW, "recover_above": RECOVER_ABOVE,
                        "floor": FACTOR_FLOOR, "min_judged": MIN_JUDGED,
                        "required_gain_bps": required_gain_bps(proposed, REVERT_EXPECTANCY_BPS),
                        "margin_bps": REVERT_EXPECTANCY_BPS,
                        "how_the_engine_applies_it": (
                            "decide multiplies the factor into each candidate's expected gain (candidate minus "
                            "baseline expectancy, per window) and requires the deflated gain to cover the "
                            "optimism's share of the revert margin: factor x gain >= (1 - factor) x margin. At 1.0 "
                            "the pre-registered rule is unchanged.")},
        "rule": RULE, "method": METHOD, "paper_only": True,
    }


def record_engine_audit(db: Session, *, now: Optional[datetime] = None) -> dict:
    """Weekly: recompute and store one ``engine_audit`` row; the factor it carries is the one the engine reads."""
    now = now or datetime.now(UTC)
    audit = build_engine_audit(db, now=now)
    calibration = audit["calibration"]
    rate = audit["hit_rate"]
    compact = [{k: r.get(k) for k in ("change_id", "strategy_type", "template", "verdict", "verdict_method",
                                      "verdict_fixed_20", "verdict_e_process", "expected_gain_bps",
                                      "realised_gain_bps", "realised_source", "forward_trades", "shortfall_bps",
                                      "realised_over_expected", "judged")} for r in audit["changes"]]
    payload = {"as_of": audit["as_of"], "calibration": calibration, "hit_rate": rate, "changes": compact,
               "method": METHOD, "paper_only": True}
    write_audit_log(
        db, event_type=EVENT, entity_type="experiment_engine", action=ACTION, status=calibration["status"],
        message=(f"engine audit: {rate['changes']} changes, {rate['judged']} judged, median realised/expected "
                 f"{calibration['ratio_median'] if calibration['ratio_median'] is not None else 'n/a'}; calibration "
                 f"factor {calibration['factor']:g} ({calibration['status']})"),
        payload=payload,
    )
    return {"status": "complete", "as_of": audit["as_of"], "changes": rate["changes"], "judged": rate["judged"],
            "ratio_median": calibration["ratio_median"], "factor": calibration["factor"],
            "previous_factor": calibration["in_force"], "calibration_status": calibration["status"],
            "paper_only": True}


# ------------------------------------------------------------ projections

TABLE_KEYS = ("change_id", "strategy_type", "template", "description", "adopted_at", "state", "verdict",
              "expected_gain_bps", "realised_gain_bps", "realised_source", "forward_trades", "shortfall_bps",
              "realised_over_expected", "judged", "verdict_method", "verdict_fixed_20", "verdict_e_process")


def _section(audit: Mapping) -> dict:
    rate, calibration = audit["hit_rate"], audit["calibration"]
    ratio = rate["realised_over_expected"]
    verdicts = rate["verdicts"]
    median_text = f"{ratio['median']:.2f}" if ratio["median"] is not None else "n/a"
    detail = (f"{rate['changes']} adopted changes ({verdicts['working']} working, {verdicts['not_helping']} not helping, "
              f"{verdicts['inconclusive']} inconclusive, {verdicts['watching']} watching); median realised/expected "
              f"{median_text} over {rate['judged']} judged; calibration factor in force {calibration['in_force']:g}"
              + (f", this audit would set {calibration['factor']:g}" if calibration["factor"] != calibration["in_force"]
                 else "") + ".")
    return {"status": "ok" if rate["changes"] else "waiting", "as_of": audit["as_of"], "detail": detail,
            "changes": [{k: r.get(k) for k in TABLE_KEYS} for r in audit["changes"]],
            "hit_rate": {"changes": rate["changes"], "judged": rate["judged"], "verdicts": verdicts,
                         "working_share": rate["working_share"], "ratio_median": ratio["median"], "ratio_ci": ratio["ci"],
                         "candidates_per_adoption": rate["multiple_testing"]["candidates_per_adoption"],
                         "luck_share_of_adoptions": rate["multiple_testing"]["luck_share_of_adoptions"],
                         "multiple_testing_method": MULTIPLE_TESTING_METHOD, "judges": rate.get("judges"),
                         "verdict_method": settings.experiment_verdict_method},
            "calibration": {"in_force": calibration["in_force"], "in_force_as_of": calibration["in_force_as_of"],
                            "proposed": calibration["factor"], "status": calibration["status"],
                            "reason": calibration["reason"], "required_gain_bps": calibration["required_gain_bps"]}}


def engine_audit_view(db: Session, *, now: Optional[datetime] = None) -> dict:
    """``GET /stock-paper/engine-audit``: the live audit plus the stored row the engine reads from."""
    audit = build_engine_audit(db, now=now)
    latest = latest_audit(db)
    return {**audit, "latest_recorded": ({"as_of": latest.get("as_of"), "calibration": latest.get("calibration"),
                                          "hit_rate": latest.get("hit_rate")} if latest else None),
            "thresholds": {"min_judged": MIN_JUDGED, "tighten_below": TIGHTEN_BELOW, "recover_above": RECOVER_ABOVE,
                           "floor": FACTOR_FLOOR, "alpha": ALPHA, "expected_window": EXPECTED_WINDOW},
            "live_authorized": False}


def weekly_report_section(db: Session, *, now: Optional[datetime] = None) -> dict:
    """The ``engine_audit`` section of the weekly report."""
    return _section(build_engine_audit(db, now=now))


def scoreboard_engine_audit(db: Session, *, now: Optional[datetime] = None) -> dict:
    """The ``engine_audit`` section of the learning scoreboard."""
    return _section(build_engine_audit(db, now=now))
