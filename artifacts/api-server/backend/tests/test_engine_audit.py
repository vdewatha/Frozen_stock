"""Engine audit: expected vs realised per change, the engine's hit rate, and the calibration factor."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.security import required_role
from app.models import AuditLog
from app.services import engine_audit as audit
from app.services import experiment_engine as engine
from app.services.audit import write_audit_log
from app.tasks.celery_app import celery_app
from tests.test_experiment_engine import _adopt, _attribution, _fake_replay, _repeat_market_losses, _rows, small_universe  # noqa: F401,E501  (fixture)
from tests.test_loss_feedback_loop import _strategy, db  # noqa: F401  (fixture)

UTC = timezone.utc
NOW = datetime(2026, 11, 8, 17, 0, tzinfo=UTC)  # Sunday
SAT = datetime(2026, 11, 14, 18, 30, tzinfo=UTC)


def _window(exp, trades=100, dd=0.1):
    return {"trades": trades, "expectancy_bps": exp, "total_profit_bps": trades * exp, "portfolio_max_drawdown": dd}


def _change(change_id="c1", *, baseline=5.0, adopted=15.0, before=4.0, strategy="channel_breakout",
            template="market_gate", before_value=None, min_trades=20, factor=None):
    expected = {"baseline": {w: _window(baseline) for w in engine.WINDOWS},
                "adopted": {w: _window(adopted, 80) for w in engine.WINDOWS}, "label": "sma20"}
    if factor is not None:
        expected["calibration_factor"] = factor
    return {"change_id": change_id, "strategy_id": 1, "strategy_type": strategy, "template": template,
            "key": "market_gate_sma", "trigger": "missed_market_move", "description": "no new longs below the 20-day",
            "before": {"market_gate_sma": before_value}, "after": {"market_gate_sma": 20},
            "adopted_at": "2026-11-01T17:00:00+00:00", "state": "watching", "expected": expected,
            "recent_before": {"trades": 20, "expectancy_bps": before, "cause_share": 0.5},
            "watch": {"min_trades": min_trades, "max_trades": 60}, "paper_only": True}


def _verdict(change_id="c1", *, after=9.0, trades=20, verdict="working", shadow=None):
    state = {"working": "confirmed", "not_helping": "reverted", "inconclusive": "closed"}[verdict]
    return {"change_id": change_id, "verdict": verdict, "state": state, "reverted": verdict == "not_helping",
            "after": {"trades": trades, "expectancy_bps": after, "cause_share": 0.2}, "shadow": shadow,
            "judged_at": "2026-11-20T22:00:00+00:00", "paper_only": True}


def _shadow(*, live_bps=8.0, previous_bps=6.0, overlap=12, previous="off"):
    return {"decisions": 30, "beating": [], "candidates": [
        {"candidate": "sma20", "is_live": True, "trades": overlap, "expectancy_bps": live_bps,
         "delta_expectancy_bps": 0.0, "overlap": overlap, "beats_live": False},
        {"candidate": previous, "is_live": False, "trades": overlap + 3, "expectancy_bps": previous_bps,
         "delta_expectancy_bps": round(previous_bps - live_bps, 2), "overlap": overlap, "beats_live": False}]}


def _write(db, action, payload, *, event=engine.EVENT, status="x", when=None):
    row = write_audit_log(db, event_type=event, entity_type="strategy", entity_id=1, action=action, status=status,
                          message=action, payload=payload)
    if when is not None:
        row.created_at = when.replace(tzinfo=None)
    db.flush()
    return row


def _experiment(adopted="sma20", applied=True, candidates=("off", "sma10", "sma20", "sma50")):
    setups = {"baseline": {}, **{c: {} for c in candidates if c != "off"}}
    return {"strategy_type": "channel_breakout", "template": "market_gate", "setups": setups,
            "adopted": adopted, "applied": applied and bool(adopted)}


# --------------------------------------------------------------- per change

def test_expected_and_realised_gain_from_the_ledger_payloads():
    record = audit.change_record(_change(), _verdict())
    assert record["expected_window"] == "2020+" and record["expected_gain_bps"] == 10.0
    assert (record["expected_baseline_bps"], record["expected_adopted_bps"], record["replay_trades"]) == (5.0, 15.0, 80)
    # Realised = after minus the last trades before the change, on the live trades.
    assert record["realised_source"] == "live" and record["realised_gain_bps"] == 5.0
    assert record["forward_trades"] == 20 and record["shortfall_bps"] == 5.0
    assert record["realised_over_expected"] == 0.5 and record["judged"]
    assert record["verdict"] == "working" and record["calibration_factor_at_adoption"] == 1.0
    # Too few forward trades: measured but not judged.
    thin = audit.change_record(_change(), _verdict(trades=7))
    assert thin["realised_over_expected"] == 0.5 and not thin["judged"]
    # Nothing realised yet: the expected side stands alone.
    fresh = audit.change_record(_change())
    assert fresh["realised_gain_bps"] is None and fresh["shortfall_bps"] is None and not fresh["judged"]
    assert fresh["forward_trades"] == 0 and fresh["verdict"] is None
    # The 2020+ window is missing: fall back to the next one and say so.
    change = _change()
    change["expected"]["adopted"].pop("2020+")
    assert audit.change_record(change)["expected_window"] == "2015+"


def test_shadow_record_fills_in_while_the_live_trades_are_too_few():
    # Shadow: adopted sma20 earned 8 bps over the same 12 decisions where the replaced value (off) earned 6.
    shadow = _shadow(live_bps=8.0, previous_bps=6.0, overlap=12)
    record = audit.change_record(_change(min_trades=10), _verdict(trades=3, verdict="not_helping", shadow=shadow))
    assert record["realised_source"] == "shadow" and record["realised_gain_bps"] == 2.0
    assert record["forward_trades"] == 12 and record["judged"] and record["realised_over_expected"] == 0.2
    assert record["shadow"]["previous"] == "off" and record["before_bps"] == 6.0 and record["after_bps"] == 8.0
    # Once the live trades reach min_trades they take over.
    live = audit.change_record(_change(min_trades=10), _verdict(trades=10, after=9.0, shadow=shadow))
    assert live["realised_source"] == "live" and live["realised_gain_bps"] == 5.0
    # A shadow record without the replaced value, or for an exit template, adds nothing.
    assert audit.shadow_gain(_shadow(previous="sma50"), "off") is None
    assert audit.shadow_gain(None, "off") is None
    exit_change = {**_change(template="stop_width"), "before": {"stop_loss_pct": 0.03}}
    assert audit.change_record(exit_change, _verdict(trades=3, shadow=shadow))["realised_source"] == "live"


def test_verdict_fields_are_the_additive_subset():
    fields = audit.verdict_fields(_change(), {"trades": 20, "expectancy_bps": 9.0, "cause_share": 0.2}, None)
    assert fields == {"expected_window": "2020+", "expected_gain_bps": 10.0, "realised_gain_bps": 5.0,
                      "realised_source": "live", "shortfall_bps": 5.0, "realised_over_expected": 0.5,
                      "calibration_factor_at_adoption": 1.0}


# ----------------------------------------------------------------- hit rate

def test_hit_rate_counts_verdicts_and_bootstraps_the_median_ratio():
    records = [audit.change_record(_change(f"c{i}", adopted=15.0), _verdict(f"c{i}", after=4.0 + r * 10.0,
                                                                                verdict=v))
               for i, (r, v) in enumerate([(0.3, "working"), (0.4, "working"), (0.5, "inconclusive"),
                                           (0.6, "working"), (0.2, "not_helping"), (1.2, "working")])]
    records.append(audit.change_record(_change("c9"), _verdict("c9", trades=4)))  # measured, not judged
    records.append(audit.change_record(_change("c10")))                             # still watching
    experiments = [_experiment() for _ in range(6)] + [_experiment(adopted=None)] * 4
    rate = audit.hit_rate(records, experiments)
    assert rate["changes"] == 8 and rate["judged"] == 6
    assert rate["verdicts"] == {"working": 5, "not_helping": 1, "inconclusive": 1, "watching": 1}
    assert rate["working_share"] == 0.625
    ratio = rate["realised_over_expected"]
    assert ratio["median"] == pytest.approx(0.45, abs=1e-6) and ratio["n"] == 6
    low, high = ratio["ci"]
    assert low <= ratio["median"] <= high and low >= 0.2 and high <= 1.2
    assert ratio["mean_shortfall_bps"] == pytest.approx(10.0 * (1 - 0.5333333), abs=0.01)
    testing = rate["multiple_testing"]
    assert testing["method"] == "bonferroni_expected_false_positives"
    assert testing["experiments"] == 10 and testing["candidates_tried"] == 30 and testing["adoptions"] == 6
    assert testing["candidates_per_adoption"] == 5.0
    assert testing["expected_false_positives"] == 1.5 and testing["luck_share_of_adoptions"] == 0.25
    # Below the minimum the interval is withheld, the median still reported.
    few = audit.hit_rate(records[:3], experiments)
    assert few["realised_over_expected"]["ci"] is None and few["realised_over_expected"]["n"] == 3
    assert audit.hit_rate([], [])["multiple_testing"]["luck_share_of_adoptions"] is None


def test_bootstrap_interval_is_reproducible_and_brackets_the_median():
    values = [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]
    first = audit.bootstrap_median_ci(values)
    assert first == audit.bootstrap_median_ci(values) and first[0] <= 0.5 <= first[1]
    assert audit.bootstrap_median_ci([0.1, 0.2]) is None


# -------------------------------------------------------------- tightening

def test_factor_tightens_below_half_recovers_above_point_eight_with_hysteresis():
    assert audit.next_factor(1.0, 0.3, judged=5) == 0.5          # below 0.5: tighten to the floor
    assert audit.next_factor(1.0, 0.45, judged=5) == 0.5         # the floor, never lower
    assert audit.next_factor(1.0, 0.3, judged=4) == 1.0          # too few judged: unchanged
    assert audit.next_factor(1.0, None, judged=9) == 1.0
    assert audit.next_factor(1.0, 0.65, judged=6) == 1.0         # never tightened, in the band: stays 1.0
    assert audit.next_factor(0.5, 0.65, judged=6) == 0.65        # tightened, in the band: follows the median
    assert audit.next_factor(0.5, 0.79, judged=6) == 0.79
    assert audit.next_factor(0.5, 0.81, judged=6) == 1.0         # recovered
    assert audit.next_factor(0.65, 0.2, judged=6) == 0.5
    assert audit.required_gain_bps(1.0, 10.0) == 0.0
    assert audit.required_gain_bps(0.5, 10.0) == 10.0
    assert audit.required_gain_bps(0.8, 10.0) == 2.5


def _ledger(db, ratios, *, start=1):
    for i, ratio in enumerate(ratios, start=start):
        _write(db, engine.ACTION_ADOPTED, _change(f"c{i}", baseline=5.0, adopted=15.0, before=4.0))
        _write(db, engine.ACTION_VERDICT, _verdict(f"c{i}", after=4.0 + ratio * 10.0,
                                                   verdict="working" if ratio >= 0.5 else "inconclusive"))
        _write(db, engine.ACTION_EXPERIMENT, _experiment())


def test_audit_row_sets_the_factor_and_the_factor_recovers(db):  # noqa: F811
    assert audit.current_calibration_factor(db) == 1.0
    _ledger(db, [0.3, 0.4, 0.2, 0.6])
    waiting = audit.record_engine_audit(db, now=SAT)
    assert waiting["calibration_status"] == "waiting" and waiting["factor"] == 1.0 and waiting["judged"] == 4
    assert audit.current_calibration_factor(db) == 1.0
    _ledger(db, [0.1], start=5)
    tightened = audit.record_engine_audit(db, now=SAT + timedelta(days=7))
    assert tightened["calibration_status"] == "tightened" and tightened["factor"] == 0.5
    assert tightened["ratio_median"] == 0.3 and tightened["previous_factor"] == 1.0
    assert audit.current_calibration_factor(db) == 0.5
    rows = db.scalars(select(AuditLog).where(AuditLog.event_type == "engine_audit")).all()
    assert len(rows) == 2 and rows[-1].status == "tightened"
    assert rows[-1].payload["calibration"]["required_gain_bps"] == 10.0
    assert rows[-1].payload["changes"][0]["realised_over_expected"] is not None
    # The ratio climbs into the band: the factor follows the median while tightened ...
    _ledger(db, [0.9, 1.0, 1.1, 0.95, 1.2, 0.7], start=6)
    band = audit.record_engine_audit(db, now=SAT + timedelta(days=14))
    assert band["ratio_median"] == 0.7 and band["factor"] == 0.7 and band["calibration_status"] == "relaxed"
    # ... and returns to 1.0 once the median recovers above 0.8.
    _ledger(db, [1.0, 1.1, 1.3], start=12)
    recovered = audit.record_engine_audit(db, now=SAT + timedelta(days=21))
    assert recovered["ratio_median"] > 0.8 and recovered["factor"] == 1.0 and recovered["calibration_status"] == "relaxed"
    assert audit.current_calibration_factor(db) == 1.0
    # A corrupt stored factor is ignored.
    _write(db, audit.ACTION, {"calibration": {"factor": 0.1}}, event=audit.EVENT)
    assert audit.current_calibration_factor(db) == 1.0


# ---------------------------------------------------------- engine applies it

def test_decide_applies_the_factor_without_changing_the_rule():
    rows = _rows(baseline=(10.0, 0.10), sma20=(13.0, 0.10), sma50=(22.0, 0.10))
    plain = engine.decide(rows)
    assert plain["adopted"] == "sma50" and plain["eligible"] == {"sma20": 3.0, "sma50": 12.0}
    assert plain["calibration"] == {"factor": 1.0, "required_gain_bps": 0.0, "margin_bps": engine.REVERT_EXPECTANCY_BPS}
    # At 0.5 a candidate must beat the baseline by 10 bps (the revert margin): sma20's 3 bps no longer passes.
    tight = engine.decide(rows, calibration=0.5)
    assert tight["adopted"] == "sma50" and tight["eligible"] == {"sma50": 6.0}
    assert "deflated gain +1.5 < 5.0 bps required at calibration 0.5" in tight["failed"]["sma20"]
    assert tight["calibration"]["required_gain_bps"] == 10.0
    assert engine.decide(_rows(baseline=(10.0, 0.10), sma20=(19.0, 0.10)), calibration=0.5)["adopted"] is None
    assert engine.decide(_rows(baseline=(10.0, 0.10), sma20=(20.0, 0.10)), calibration=0.5)["adopted"] == "sma20"
    # The other tests of the rule are untouched by the factor.
    assert "only 20 trades" in engine.decide(_rows(baseline=(10.0, 0.10, 100), sma20=(40.0, 0.10, 20)),
                                             calibration=0.5)["failed"]["sma20"]
    assert engine.decide(rows, calibration=0.5)["rule"] == engine.RULE


def test_engine_reads_the_factor_and_documents_it_in_the_ledger(db, monkeypatch, small_universe):  # noqa: F811
    row = _strategy(db, "channel_breakout")
    _write(db, audit.ACTION, {"calibration": {"factor": 0.5, "status": "tightened"}, "as_of": "2026-11-07"},
           event=audit.EVENT)
    _repeat_market_losses(db, "channel_breakout")
    # A 7 bps replay gain passed at factor 1.0 (test_experiment_engine); at 0.5 it needs 10.
    simulate = _fake_replay(monkeypatch, {"baseline": (5.0, 0.20, 200), "sma20": (12.0, 0.15, 150)})
    result = engine.run_experiment_engine(db, now=NOW, simulate=simulate, **small_universe)
    assert result["calibration_factor"] == 0.5 and result["adopted"] == []
    experiment = engine.experiment_history(db)[0]
    assert experiment["calibration"] == {"factor": 0.5, "required_gain_bps": 10.0, "margin_bps": 10.0}
    assert "calibration 0.5" in experiment["decision"]["failed"]["sma20"]
    db.refresh(row)
    assert "market_gate_sma" not in row.parameters
    # A candidate that clears the deflated hurdle is adopted, and the adoption records the factor.
    simulate = _fake_replay(monkeypatch, {"baseline": (5.0, 0.20, 200), "sma20": (16.0, 0.15, 150)})
    monkeypatch.setattr(settings, "experiment_engine_cooldown_days", 0)
    _repeat_market_losses(db, "channel_breakout")
    engine.run_experiment_engine(db, now=NOW + timedelta(days=1), simulate=simulate, **small_universe)
    change = engine.adopted_changes(db)[0]
    assert change["expected"]["calibration_factor"] == 0.5 and change["expected"]["required_gain_bps"] == 10.0
    assert audit.change_record(change)["calibration_factor_at_adoption"] == 0.5


def test_verdict_carries_expected_vs_realised(db, monkeypatch, small_universe):  # noqa: F811
    monkeypatch.setattr(settings, "experiment_verdict_min_trades", 5)
    row = _strategy(db, "channel_breakout")
    _adopt(db, row, monkeypatch, small_universe)
    for i in range(5):
        _attribution(db, strategy_type="channel_breakout", cause="edge", net_return=0.0005,
                     closed_at=NOW + timedelta(days=2 + i), opened_at=NOW + timedelta(days=1 + i))
    verdict = engine.review_changes(db, now=NOW + timedelta(days=8))["reviewed"][0]
    assert verdict["expected_gain_bps"] == 7.0 and verdict["realised_source"] == "live"
    assert verdict["realised_gain_bps"] == pytest.approx(5.0 - verdict["before"]["expectancy_bps"], abs=0.01)
    assert verdict["shortfall_bps"] == pytest.approx(7.0 - verdict["realised_gain_bps"], abs=0.01)
    stored = db.scalars(select(AuditLog).where(AuditLog.action == engine.ACTION_VERDICT)).all()[-1]
    assert stored.payload["realised_over_expected"] == verdict["realised_over_expected"]
    records = audit.change_records(db)
    assert len(records) == 1 and records[0]["judged"] and records[0]["verdict"] == "working"


# ------------------------------------------------------------------ surface

def test_views_report_and_scoreboard_carry_the_section(db, monkeypatch):  # noqa: F811
    from app.services.learning_scoreboard import build_learning_scoreboard
    from app.services.weekly_report import build_weekly_report

    monkeypatch.setattr("app.services.stock_paper_ledger.active_paper_broker_name", lambda: "alpaca_paper")
    empty = audit.engine_audit_view(db, now=SAT)
    assert empty["changes"] == [] and empty["calibration"]["status"] == "waiting" and empty["latest_recorded"] is None
    assert empty["rule"] == engine.RULE and empty["thresholds"]["min_judged"] == 5 and not empty["live_authorized"]
    _ledger(db, [0.3, 0.4, 0.2, 0.6, 0.1])
    audit.record_engine_audit(db, now=SAT)
    view = audit.engine_audit_view(db, now=SAT)
    assert view["calibration"]["in_force"] == 0.5 and view["latest_recorded"]["calibration"]["factor"] == 0.5
    assert len(view["changes"]) == 5 and view["hit_rate"]["judged"] == 5
    section = build_weekly_report(db, now=SAT)["engine_audit"]
    assert section["status"] == "ok" and section["calibration"]["in_force"] == 0.5
    assert section["hit_rate"]["ratio_median"] == 0.3 and section["hit_rate"]["luck_share_of_adoptions"] == 0.15
    assert set(section["changes"][0]) == set(audit.TABLE_KEYS)
    assert "median realised/expected 0.30 over 5 judged; calibration factor in force 0.5" in section["detail"]
    board = build_learning_scoreboard(db, now=SAT)["engine_audit"]
    assert board["status"] == "ok" and board["changes"][0]["change_id"] == "c5"
    assert board["hit_rate"]["verdicts"] == {"working": 1, "not_helping": 0, "inconclusive": 4, "watching": 0}


def test_job_route_queue_and_beat_are_wired():
    from app.tasks import jobs

    beat = celery_app.conf.beat_schedule
    assert beat["engine-audit"]["task"] == "app.tasks.jobs.engine_audit_job"
    schedule = beat["engine-audit"]["schedule"]
    assert schedule.hour == {14} and schedule.minute == {30} and schedule.day_of_week == {6}
    report = beat["weekly-performance-report"]["schedule"]
    assert report.day_of_week == {6} and (min(report.hour), min(report.minute)) < (min(schedule.hour), min(schedule.minute))
    assert celery_app.conf.task_routes["app.tasks.jobs.engine_audit_job"] == {"queue": "learning"}
    assert required_role("GET", "/stock-paper/engine-audit") == "viewer"
    assert callable(jobs.engine_audit_job)
