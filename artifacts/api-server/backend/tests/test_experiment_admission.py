"""Admission requirement for new experiments: can it reach a decision in a useful time?"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models import AuditLog, Strategy
from app.services import experiment_admission as admission
from app.services import experiment_engine as engine
from app.services import forward_experiments as fx
from app.services import hypothesis_register as hr
from app.services import strategy_discovery as sd
from tests.test_forward_experiments import _ibs
from tests.test_loss_feedback_loop import db  # noqa: F401  (fixture)
from tests.test_strategy_discovery import _row

UTC = timezone.utc
NOW = datetime(2026, 11, 16, 15, 0, tzinfo=UTC)
WINDOW = {"holdout_start": "2024-11-16", "data_end": "2026-11-16"}  # a two-year holdout


def _decisions(db):
    return db.scalars(select(AuditLog).where(AuditLog.event_type == admission.EVENT).order_by(AuditLog.id)).all()


# ------------------------------------------------------------- the estimate

def test_the_estimate_reproduces_the_frozen_spec_s_own_table():
    """The spec's inputs (270 bps, rho 0.15, effect 30 bps, t 2) give its verdict samples and its years."""
    spec = fx.load_spec()
    years, derived = {}, {}
    for candidate in spec["candidates"]:
        number = int(candidate["candidate"])
        free = admission.estimate(trades_per_year=candidate["expected_trades_per_year"], symbols=len(candidate["symbols"]),
                                  per_trade_std_bps=admission.FROZEN_SPEC_STD_BPS, rho=admission.FROZEN_SPEC_RHO)
        derived[number] = (free["independent_samples_required"], free["design_effect"], free["verdict_sample"])
        check = admission.check_forward_candidate({**candidate, "verdict_sample": fx.VERDICT_SAMPLE[number]})
        years[number] = check["estimate"]["years_to_verdict"]
        assert check["admitted"] is False and "years to a verdict" in check["reasons"][0]
    # n = (2 x 270 / 30)^2 = 324 independent trades; eleven a date at rho 0.15 is a design effect of 2.5.
    assert derived[1] == (324, 2.5, 810) and derived[4] == (324, 1.45, 470)
    assert years == {1: 47.1, 2: 17.8, 3: 66.7, 4: 55.6, 5: 10.1, 6: 2.2}  # the report: ~45, ~18, ~65, ~55, ~10, ~2


def test_an_experiment_s_own_clock_bounds_its_time_to_a_decision():
    spec = fx.load_spec()
    six = next(c for c in spec["candidates"] if int(c["candidate"]) == 6)
    check = admission.check_forward_candidate({**six, "verdict_sample": 800, "decision_deadline_years": 2.0})
    assert check["admitted"] is True and check["estimate"]["years_to_verdict"] == 2.0
    assert check["estimate"]["years_to_verdict_sample"] == 2.2
    unknown = admission.estimate(trades_per_year=None, symbols=3, per_trade_std_bps=300, rho=0.07)
    assert unknown["years_to_verdict"] is None and unknown["trades_per_year"] is None
    fixed = admission.estimate(trades_per_year=40, symbols=1, per_trade_std_bps=300, rho=0.07, same_date_trades=1.0,
                               sample_required=20)
    assert fixed["verdict_sample"] == 20 and fixed["design_effect"] == 1.0 and fixed["years_to_verdict"] == 0.5


# ------------------------------------------------------------- the decision

def test_a_closed_family_needs_new_evidence_named_in_the_register():
    register = hr.load_register()
    fast = admission.estimate(trades_per_year=2000, symbols=2, per_trade_std_bps=300, rho=0.07)
    closed = admission.decide({"kind": "discovery_rule", "key": "x1", "family": "earnings_drift"}, fast, register=register)
    assert closed["admitted"] is False and closed["decision"] == "refused"
    assert closed["reasons"] == ["family post_earnings_drift is closed by earnings-drift; a new experiment must name "
                                 "its new evidence in the hypothesis register"]
    assert closed["family_closure"]["asked_as"] == "earnings_drift"
    reopened = {**register, "admission_overrides": {"earnings_drift": {"new_evidence": "intraday reaction data"}}}
    again = admission.decide({"kind": "discovery_rule", "key": "x1", "family": "earnings_drift"}, fast, register=reopened)
    assert again["admitted"] is True and again["family_closure"]["new_evidence"] == "intraday reaction data"
    # A family the register knows nothing about, or one with an adopted study, is not closed.
    assert hr.family_closure("pullback_in_uptrend") is None and hr.family_closure(None) is None
    assert hr.family_closure("market_timing_gates")["closed"] is False
    assert hr.family_closure("down_streak")["family"] == "dip_buying" and hr.family_closure("down_streak")["closed"]
    for alias, family in register["family_aliases"].items():
        assert any(entry["family"] == family for entry in register["entries"]), alias


def test_a_slow_experiment_is_refused_an_unknown_rate_is_noted_and_the_budget_defers(monkeypatch):
    slow = admission.estimate(trades_per_year=15, symbols=2, per_trade_std_bps=300, rho=0.07)
    refused = admission.decide({"kind": "discovery_rule", "key": "s1", "family": None}, slow)
    assert refused["decision"] == "refused" and refused["estimate"]["verdict_sample"] == 428
    assert refused["reasons"] == ["about 28.5 years to a verdict (428 trades at 15 a year); the limit is 2"]
    unknown = admission.decide({"kind": "engine_experiment", "key": "a:b", "family": None},
                               admission.estimate(trades_per_year=None, symbols=1, per_trade_std_bps=300, rho=0.07))
    assert unknown["admitted"] is True and unknown["note"].startswith("trade rate unknown")
    fast = admission.estimate(trades_per_year=2000, symbols=2, per_trade_std_bps=300, rho=0.07)
    monkeypatch.setattr(settings, "experiment_admission_compute_minutes", 3.0)
    first = admission.decide({"kind": "engine_experiment", "key": "a:b", "family": None}, fast)
    second = admission.decide({"kind": "engine_experiment", "key": "c:d", "family": None}, fast,
                              compute_spent_minutes=first["compute_minutes"])
    assert first["admitted"] and first["compute_minutes"] == 2.0
    assert second["decision"] == "deferred" and "compute budget (2 + 2 of 3 minutes)" in second["reasons"][0]
    monkeypatch.setattr(settings, "experiment_admission_max_years", 30.0)
    assert admission.decide({"kind": "discovery_rule", "key": "s1", "family": None}, slow)["admitted"] is True


# ------------------------------------------------------------- discovery

def test_discovery_gives_no_row_to_a_rule_that_cannot_decide_in_time_or_sits_in_a_closed_family(db):  # noqa: F811
    slow, closed, fast = _row("slow1", "proven"), _row("shut1", "proven", family="down_streak"), _row("fast1", "explore")
    closed["holdout"]["trades"] = fast["holdout"]["trades"] = 2000
    result = {"candidates": [slow, closed, fast], **WINDOW}
    outcome = sd.admit_survivors(db, result, now=NOW)
    assert outcome["admitted"] == ["rule_fast1"]
    refused = {item["slug"]: item for item in outcome["refused"]}
    assert set(refused) == {"rule_slow1", "rule_shut1"}
    assert refused["rule_slow1"]["years_to_verdict"] == 28.5 and "the limit is 2" in refused["rule_slow1"]["reasons"][0]
    assert refused["rule_shut1"]["reasons"] == ["family dip_buying is closed by dip-rules-full-market; a new experiment "
                                                "must name its new evidence in the hypothesis register"]
    assert {row.strategy_type for row in db.scalars(select(Strategy)).all()} == {"rule_fast1"}
    rows = _decisions(db)
    assert [(r.action, r.payload["key"], r.status) for r in rows] == [
        ("refuse", "slow1", "refused"), ("refuse", "shut1", "refused"), ("admit", "fast1", "admitted")]
    assert rows[0].payload["estimate"]["rate_basis"] == "holdout window" and rows[0].payload["estimate"]["trades_per_year"] == 15.0
    # The same decision is not written again next week; a changed one is.
    sd.admit_survivors(db, result, now=NOW + timedelta(days=7))
    assert len(_decisions(db)) == 3
    slow["holdout"]["trades"] = 2000
    later = sd.admit_survivors(db, result, now=NOW + timedelta(days=14))
    assert later["admitted"] == ["rule_slow1"] and [r.payload["key"] for r in _decisions(db)][-1] == "slow1"
    assert _decisions(db)[-1].action == "admit" and len(_decisions(db)) == 4


def test_discovery_admission_never_rejudges_a_row_it_already_has_and_can_be_switched_off(db, monkeypatch):  # noqa: F811
    slow = _row("slow1", "proven")
    result = {"candidates": [slow], **WINDOW}
    monkeypatch.setattr(settings, "experiment_admission_enabled", False)
    assert sd.admit_survivors(db, result, now=NOW)["admitted"] == ["rule_slow1"] and _decisions(db) == []
    monkeypatch.setattr(settings, "experiment_admission_enabled", True)
    outcome = sd.admit_survivors(db, result, now=NOW)
    assert outcome["refused"] == [] and outcome["refreshed"] == ["rule_slow1"] and outcome["retired"] == []
    assert db.scalars(select(Strategy)).one().current_status == "paper_trading_candidate"
    # A check that cannot run never stops the weekly pass: the rule is admitted as before.
    monkeypatch.setattr(admission, "check_discovery", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    other = {"candidates": [_row("other1", "proven")], **WINDOW}
    assert sd.admit_survivors(db, other, now=NOW)["admitted"] == ["rule_other1"]


# ---------------------------------------------------------------- the engine

def _closed(strategy_type, days_ago):
    return SimpleNamespace(strategy_type=strategy_type, closed_at=NOW - timedelta(days=days_ago))


def test_trade_rates_are_per_year_and_a_young_book_is_measured_over_its_own_age():
    old = [_closed("a", 170), _closed("a", 100), _closed("a", 5), _closed("b", 5), _closed("c", 400)]
    rates = admission.trade_rates(old, now=NOW)
    assert rates == {"a": pytest.approx(3 * 365.25 / 170), "b": pytest.approx(365.25 / 170)}  # c is outside the window
    young = [_closed("a", 20), _closed("a", 10), _closed("a", 1)]
    assert admission.trade_rates(young, now=NOW) == {"a": pytest.approx(3 * 365.25 / 30)}  # 30 days at least
    assert admission.trade_rates([], now=NOW) == {}
    assert admission.trade_rates([SimpleNamespace(strategy_type="a", closed_at=None)], now=NOW) == {}


def test_the_engine_skips_an_experiment_whose_forward_verdict_would_take_too_long(db, monkeypatch):  # noqa: F811
    attributions = ([_closed("busy", day) for day in range(1, 171, 5)]      # 34 trades in 170 days: ~73 a year
                    + [_closed("slow", 170), _closed("slow", 60)])          # 2 trades in 170 days: ~4 a year
    plan = {"planned": [{"strategy_type": "busy", "template": "vix_gate", "lesson_count": 5},
                        {"strategy_type": "slow", "template": "vix_gate", "lesson_count": 4},
                        {"strategy_type": engine.BOOK, "template": "position_cap", "lesson_count": 3},
                        {"strategy_type": "quiet", "template": "vix_gate", "lesson_count": 3}],
            "deferred": [], "skipped": [{"strategy_type": "x", "template": "y", "reason": "earlier"}], "untemplated": []}
    out = engine.apply_admission(db, plan, attributions, now=NOW)
    assert [item["strategy_type"] for item in out["planned"]] == ["busy", engine.BOOK, "quiet"]
    assert out["planned"][0] == plan["planned"][0]  # an admitted experiment is passed on unchanged
    skipped = out["skipped"][-1]
    assert out["skipped"][0]["reason"] == "earlier" and skipped["strategy_type"] == "slow"
    assert skipped["reason"] == "admission: about 4.7 years to a verdict (20 trades at 4.3 a year); the limit is 2"
    assert skipped["admission"]["verdict_sample"] == settings.experiment_verdict_min_trades
    assert skipped["admission"]["decision_cap_trades"] == settings.experiment_verdict_max_trades
    recorded = {row.payload["key"]: row for row in _decisions(db)}
    assert recorded["slow:vix_gate"].action == "refuse" and recorded["busy:vix_gate"].action == "admit"
    # The book's rate is every strategy's trades; a strategy with no closed trade has no rate and is noted, not refused.
    assert recorded["book:position_cap"].payload["estimate"]["trades_per_year"] == pytest.approx(36 * 365.25 / 170, abs=0.1)
    assert recorded["quiet:vix_gate"].payload["note"].startswith("trade rate unknown")
    # This run's compute budget defers what does not fit; a deferral is not recorded.
    monkeypatch.setattr(settings, "experiment_admission_compute_minutes", 3.0)
    tight = engine.apply_admission(db, plan, attributions, now=NOW)
    assert [item["strategy_type"] for item in tight["planned"]] == ["busy"]
    assert [item["strategy_type"] for item in tight["deferred"]] == [engine.BOOK, "quiet"]
    assert "compute budget" in tight["deferred"][0]["reason"] and len(_decisions(db)) == 4
    # Off, or unable to run, it changes nothing.
    monkeypatch.setattr(admission, "trade_rates", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert engine.apply_admission(db, plan, attributions, now=NOW) is plan
    monkeypatch.setattr(settings, "experiment_admission_enabled", False)
    assert engine.apply_admission(db, plan, attributions, now=NOW) is plan
    assert engine.apply_admission(db, {**plan, "planned": []}, attributions, now=NOW)["planned"] == []


# ------------------------------------------------------- forward experiments

def test_the_frozen_spec_is_grandfathered_and_a_later_spec_must_pass_admission(db, monkeypatch):  # noqa: F811
    _ibs(db)
    out = fx.register_forward_experiments(db, now=NOW)
    assert len(out["registered"]) == 6 and "refused" not in out and _decisions(db) == []
    for row in db.scalars(select(AuditLog).where(AuditLog.event_type == fx.EVENT)).all():
        db.delete(row)
    db.flush()
    # The same candidates under a spec that is not grandfathered: none can reach its verdict within the limit.
    monkeypatch.setattr(fx, "GRANDFATHERED_SPECS", frozenset())
    later = fx.register_forward_experiments(db, now=NOW)
    assert later["registered"] == [] and [item["candidate"] for item in later["refused"]] == [1, 2, 3, 4, 5, 6]
    assert later["refused"][1]["years_to_verdict"] == 17.8 and "the limit is 2" in later["refused"][1]["reasons"][0]
    assert [row.action for row in _decisions(db)] == ["refuse"] * 6
    monkeypatch.setattr(settings, "experiment_admission_enabled", False)
    assert len(fx.register_forward_experiments(db, now=NOW)["registered"]) == 6


# ------------------------------------------------------------------ the view

def test_the_view_shows_the_rule_the_budget_the_decisions_and_the_six_frozen_experiments(db):  # noqa: F811
    from app.api.stock_paper import router
    from app.core.security import required_role

    sd.admit_survivors(db, {"candidates": [_row("slow1", "proven")], **WINDOW}, now=NOW)
    view = admission.admission_view(db)
    assert view["enabled"] is True and view["paper_only"] is True
    assert view["budget"] == {"max_years_to_verdict": 2.0, "compute_minutes_per_run": 60.0, "minutes_per_experiment": 2.0}
    assert view["rule"]["effect_floor_bps"] == 30.0 and view["rule"]["inputs"]["per_trade_std_bps"] == 300.0
    assert view["counts"] == {"admitted": 0, "refused": 1} and view["decisions"][0]["key"] == "slow1"
    frozen = {item["candidate"]: item for item in view["frozen_forward_experiments"]}
    assert [n for n, item in frozen.items() if item["would_be_admitted_today"]] == [6]
    assert frozen[1]["estimate"]["years_to_verdict"] == 47.1 and frozen[6]["estimate"]["years_to_verdict"] == 2.0
    assert all(item["status"].startswith("grandfathered") for item in frozen.values())
    paths = {route.path: route.methods for route in router.routes}
    assert "GET" in paths["/stock-paper/experiment-admissions"]
    assert required_role("GET", "/stock-paper/experiment-admissions") == "viewer"
