"""The yardstick (step 1 of the feasibility report): cost bracket, turnover, short-hold veto, excess and residual
scoring, trials per family, monthly-sleeve guardrail, cost probes and the dividend check."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from app.core.config import settings
from app.core.security import READ_PATHS, required_role
from app.models import AuditLog, Strategy
from app.services import cost_probes, dividend_check, sleeve_guardrail, strategy_discovery, yardstick
from app.services import experiment_engine as engine
from app.services.audit import write_audit_log
from app.tasks.celery_app import celery_app
from tests.test_loss_feedback_loop import _strategy, db  # noqa: F401  (fixture)
from tests.test_meta_label import _walk

UTC = timezone.utc


def _frames(n=900, seed=1):
    return {"AAA": _walk(rows=n, seed=seed), "SPY": _walk(rows=n, seed=seed + 1)}


def _trades(frames, *, symbol="AAA", count=60, hold=3, gross=0.004, start=300, step=9):
    frame = frames[symbol]
    rows = []
    for k in range(count):
        i = start + k * step
        j = i + hold
        rows.append({"symbol": symbol, "entry_idx": i, "exit_idx": j, "decision_date": frame["date"].iloc[i - 1],
                     "entry_date": frame["date"].iloc[i], "exit_date": frame["date"].iloc[j],
                     "holding_days": hold, "barrier": "vertical", "gross_return": gross,
                     "net_return": gross - 0.003,
                     "entry_price": float(frame["open"].iloc[i]),
                     "exit_price": float(frame["open"].iloc[i]) * (1 + gross - 0.003)})
    return pd.DataFrame(rows)


# --------------------------------------------------------------- cost bracket

def test_bracket_prices_every_trade_at_flat_costs_the_model_and_calibrated_cells():
    frames = _frames()
    trades = _trades(frames)
    bracket = yardstick.cost_bracket(trades, frames, windows={"all": (None, None)})
    assert bracket["status"] == "ok"
    assert bracket["columns"] == ["5bps", "10bps", "25bps", "model", "acting"]  # no calibrated cell yet
    row = bracket["windows"]["all"]
    assert row["gross"]["expectancy_bps"] == pytest.approx(40.0)
    assert row["5bps"]["expectancy_bps"] == pytest.approx(35.0)
    assert row["25bps"]["expectancy_bps"] == pytest.approx(15.0)
    assert row["acting"]["expectancy_bps"] == pytest.approx(10.0)
    # The published model: 5 bps + 5% of daily volatility (+ impact) a side and a 1 bp fee a side.
    model_cost = bracket["costs_bps"]["model"]
    assert model_cost > 12.0
    assert row["model"]["expectancy_bps"] == pytest.approx(40.0 - model_cost, abs=0.5)
    # A flat column's interval is the gross interval shifted by the cost (exact for a constant charge).
    gross_ci, flat_ci = row["gross"]["expectancy_ci_bps"], row["10bps"]["expectancy_ci_bps"]
    assert flat_ci == [pytest.approx(gross_ci[0] - 10), pytest.approx(gross_ci[1] - 10)]
    # A cell with 20+ fills adds the calibrated column; one with fewer does not.
    thin = [{"symbol_class": "single_stock", "time_bucket": "all", "multiplier": 0.5, "fills": 12, "fitted": True}]
    assert "calibrated" not in yardstick.cost_bracket(trades, frames, windows={"all": (None, None)},
                                                      cells=yardstick.calibrated_cells({"cells": thin}))["columns"]
    cells = [{"symbol_class": "single_stock", "time_bucket": "all", "multiplier": 0.5, "fills": 25, "fitted": True}]
    calibrated = yardstick.cost_bracket(trades, frames, windows={"all": (None, None)},
                                        cells=yardstick.calibrated_cells({"cells": cells}))
    assert "calibrated" in calibrated["columns"]
    assert calibrated["costs_bps"]["calibrated"] < model_cost
    assert calibrated["calibrated_cells"][0]["fills"] == 25


def test_bracket_is_unavailable_without_gross_returns():
    assert yardstick.cost_bracket(pd.DataFrame({"gate": [1]}), {}, windows={"all": None})["status"] == "unavailable"


def test_bracket_decisions_show_which_label_the_rule_adopts_at_each_cost():
    frames = _frames()
    base = _trades(frames, gross=0.004)
    cand = _trades(frames, gross=0.006)
    rows = {label: {w: {"trades": 100, "expectancy_bps": 1.0, "total_profit_bps": 100.0, "portfolio_max_drawdown": 0.1}
                    for w in engine.WINDOWS} for label in ("baseline", "sma20")}
    windows = {w: (None, None) for w in engine.WINDOWS}
    brackets = {"baseline": yardstick.cost_bracket(base, frames, windows=windows, ci_windows=()),
                "sma20": yardstick.cost_bracket(cand, frames, windows=windows, ci_windows=())}
    decisions = yardstick.bracket_decisions(rows, brackets, engine.decide)
    assert decisions["5bps"] == "sma20" and decisions["model"] == "sma20"


# ------------------------------------------------------------- turnover, veto

def test_turnover_is_trades_times_slot_per_year():
    trades = pd.DataFrame({"x": range(440)})
    result = yardstick.turnover(trades, start="2015-01-01", end="2026-01-01", slot_fraction=0.05)
    assert result["one_sided_turnover_per_year"] == pytest.approx(440 * 0.05 / 11.0, abs=0.05)
    assert result["cost_drag_pct_per_year"]["10bps"] == pytest.approx(result["one_sided_turnover_per_year"] * 0.1,
                                                                      abs=0.01)


def test_short_hold_veto_is_the_pre_registered_rule():
    frames = _frames()
    oos = frames["AAA"]["date"].iloc[500]
    short_weak = _trades(frames, hold=3, gross=0.0025)
    veto = yardstick.short_hold_veto(short_weak, oos_start=oos)
    assert veto["vetoed"] and veto["required_gross_bps"] == 30.0 and "< 30.0 required" in veto["reason"]
    assert not yardstick.short_hold_veto(_trades(frames, hold=3, gross=0.0035), oos_start=oos)["vetoed"]
    # A measured round trip of 15 bps raises the bar to 3 x 15 = 45 bps.
    assert yardstick.short_hold_veto(_trades(frames, hold=3, gross=0.0040), oos_start=oos, cost_bps=15)["vetoed"]
    # A median hold of 20 sessions or more is never vetoed.
    long_weak = _trades(frames, hold=20, gross=0.0001, step=25, count=20)
    assert not yardstick.short_hold_veto(long_weak, oos_start=oos)["vetoed"]
    # No out-of-sample trade cannot clear the bar.
    assert yardstick.short_hold_veto(short_weak, oos_start="2099-01-01")["vetoed"]
    assert yardstick.short_hold_veto(pd.DataFrame({"gate": [1]}), oos_start=oos)["status"] == "unavailable"
    assert yardstick.veto_cost_bps({}, ["index_etf"]) == {"cost_bps": 10.0, "source": "default_10bps"}
    assert yardstick.veto_cost_bps({"index_etf": 4.0, "single_stock": 12.0}, ["index_etf", "single_stock"]) == \
        {"cost_bps": 12.0, "source": "measured"}


def _window(exp, dd, n=100):
    return {"trades": n, "expectancy_bps": exp, "total_profit_bps": n * exp, "portfolio_max_drawdown": dd}


def test_decide_applies_the_short_hold_veto_and_records_it(monkeypatch):
    rows = {"baseline": {w: _window(5.0, 0.2) for w in engine.WINDOWS},
            "sma20": {w: _window(12.0, 0.15) for w in engine.WINDOWS}}
    vetoes = {"sma20": {"vetoed": True, "reason": "median hold 3 sessions; out-of-sample gross +12.0 bps < 30.0"}}
    decision = engine.decide(rows, vetoes=vetoes)
    assert decision["adopted"] is None and decision["failed"]["sma20"].startswith("short-hold veto")
    assert decision["short_hold_veto"] == {"enforced": True, "vetoed": {"sma20": vetoes["sma20"]["reason"]}}
    assert "Short-hold veto" in engine.RULE and engine.RULE.startswith("Adopt a candidate only if, in every window")
    monkeypatch.setattr(settings, "yardstick_short_hold_veto", False)
    relaxed = engine.decide(rows, vetoes=vetoes)
    assert relaxed["adopted"] == "sma20" and relaxed["short_hold_veto"]["enforced"] is False


def test_engine_experiment_records_the_yardstick_and_vetoes_short_holds(monkeypatch):
    frames = _frames()
    frames["AAA"] = frames["AAA"].assign(date=pd.bdate_range("2019-01-02", periods=900).strftime("%Y-%m-%d"))
    frames["SPY"] = frames["SPY"].assign(date=frames["AAA"]["date"])
    monkeypatch.setattr(settings, "experiment_multiplicity_hurdle", False)

    def simulate(strategy, frames_, spy, setup, replays, vix=None, **_):
        gross = 0.0012 if setup.get("market_gate_sma") else 0.0008
        return _trades(frames_, gross=gross, count=90, step=6)

    row = Strategy(id=7, name="x", strategy_type="channel_breakout", parameters={}, current_status="paper_trading_active")
    experiment = engine.run_experiment(row, engine.TEMPLATE_BY_NAME["market_gate"], frames=frames, spy=frames["SPY"],
                                       vix=None, replays={}, simulate=simulate, family_trials=7)
    assert experiment["family_trials"] == 7
    readings = experiment["yardstick"]
    assert set(readings["scores"]) == set(experiment["setups"])
    assert readings["scores"]["sma20"]["turnover"]["status"] == "ok"
    assert readings["vetoes"]["sma20"]["vetoed"]  # 3-session holds, 12 bps gross < 30
    assert all(str(reason).startswith("short-hold veto") or "trades" in str(reason) or "expectancy" in str(reason)
               for reason in experiment["decision"]["failed"].values())
    assert experiment["adopted"] is None
    assert "5bps" in readings["bracket_decisions"]


# ------------------------------------------------------- excess and residual

def test_passive_scores_find_no_excess_in_a_scaled_copy_of_spy_and_find_real_alpha():
    n = 1500
    dates = pd.bdate_range("2015-01-02", periods=n)
    rng = np.random.default_rng(3)
    spy_ret = rng.normal(0.0004, 0.01, n)
    close = 100 * np.cumprod(1 + spy_ret)
    spy = pd.DataFrame({"date": dates.strftime("%Y-%m-%d"), "open": close, "high": close, "low": close,
                        "close": close, "volume": 1e6})
    trades = pd.DataFrame({"symbol": ["SPY"] * 50, "decision_date": dates[100:150].strftime("%Y-%m-%d"),
                           "entry_date": dates[101:151].strftime("%Y-%m-%d"),
                           "exit_date": dates[103:153].strftime("%Y-%m-%d"), "barrier": "vertical",
                           "net_return": rng.normal(0.0, 0.01, 50)})
    copy = pd.Series(0.5 * spy_ret, index=dates)
    flat = yardstick.passive_scores(trades, {"SPY": spy}, spy, windows={"all": (None, None)}, portfolio=copy)
    excess = flat["windows"]["all"]["excess_vs_passive"]
    assert excess["spy_weight"] == pytest.approx(0.5, abs=0.01)
    assert abs(excess["mean_active_bps_per_day"]) < 0.5 and not excess["passes"]
    assert flat["tbill"].startswith("unavailable")
    alpha = pd.Series(0.5 * spy_ret + 0.002 + rng.normal(0, 0.001, n), index=dates)
    irx = pd.DataFrame({"date": dates.strftime("%Y-%m-%d"), "close": 2.0})
    strong = yardstick.passive_scores(trades, {"SPY": spy}, spy, windows={"all": (None, None)}, portfolio=alpha,
                                      irx=irx)
    assert strong["windows"]["all"]["excess_vs_passive"]["passes"]
    assert strong["tbill"].startswith("13-week")
    assert not strong["windows"]["all"]["spy_residual"]["passes"]  # the trades themselves are noise
    assert not strong["windows"]["all"]["passes"]                   # both must pass


def test_clustered_t_reuses_the_cr0_estimator():
    values = np.array([0.01, 0.01, -0.01, -0.01, 0.02, 0.02])
    clustered = yardstick.clustered_t(values, ["a", "a", "b", "b", "c", "c"])
    naive = yardstick.clustered_t(values, ["a", "b", "c", "d", "e", "f"])
    assert clustered["clusters"] == 3 and naive["clusters"] == 6
    assert abs(clustered["t_stat"]) < abs(naive["t_stat"])


# ------------------------------------------------------------ trial families

def test_trials_per_family_combine_discovery_engine_and_research(db):  # noqa: F811
    strategy_discovery.record_trials(db, hashes=["h1", "h2", "h3"], retained=[], runs=1, data_end="2026-11-06",
                                     hash_families={"h1": "sector_reversal", "h2": "sector_reversal",
                                                    "h3": "pullback_in_uptrend"})
    write_audit_log(db, event_type=engine.EVENT, entity_type="strategy", action=engine.ACTION_EXPERIMENT,
                    status="rejected", message="x",
                    payload={"strategy_type": "rsi2_pullback", "setups": {"baseline": {}, "a": {}, "b": {}}})
    db.commit()
    families = yardstick.trial_families(db)["families"]
    assert families["sector_reversal"] == {"discovery": 2, "engine": 0, "research": 9, "total": 11}
    assert families["pullback_in_uptrend"]["discovery"] == 1 and families["pullback_in_uptrend"]["engine"] == 2
    assert yardstick.external_family_trials(db, exclude="discovery")["pullback_in_uptrend"] == 2
    assert yardstick.external_family_trials(db, exclude="engine")["sector_reversal"] == 11
    ledger = engine.experiment_ledger(db)
    assert ledger["yardstick"]["trial_families"]["families"]["sector_reversal"]["total"] == 11


def test_family_trials_only_make_the_deflated_sharpe_stricter():
    summary = {"sharpe_per_period": 0.08, "observations": 2000, "skewness": 0.0, "kurtosis": 3.0,
               "discovery": {"net": {"trades": 300, "expectancy_ci_bps": [1, 9]},
                             "excess_over_drift": {"expectancy_ci_bps": [1, 5]}},
               "post_period": {"trades": 100, "expectancy_bps": 3.0}, "holdout": {"trades": 40, "expectancy_bps": 2.0}}
    plain = strategy_discovery.gate(summary, trials=50, trial_sharpes=[0.01, 0.03, 0.05, 0.08])
    charged = strategy_discovery.gate(summary, trials=50, trial_sharpes=[0.01, 0.03, 0.05, 0.08], family_trials=300)
    assert charged["dsr_trials"] == 350 and charged["library_trials"] == 50 and charged["family_trials"] == 300
    assert charged["deflated_sharpe"] <= plain["deflated_sharpe"]


def test_discovery_admission_vetoes_new_short_hold_candidates_but_never_retires(db):  # noqa: F811
    def row(cid, vetoed):
        return {"id": cid, "family": "n_day_low", "universe": "index_etfs", "description": f"rule {cid}",
                "rule": {"entry": [], "exit": []}, "stop_loss_pct": 0.1, "take_profit_pct": 0.0,
                "max_holding_sessions": 10, "symbols": ["SPY"], "discovery": {}, "post_period": {"expectancy_bps": 1},
                "holdout": {"trades": 30, "win_rate": 0.5, "expectancy_bps": 2},
                "gate": {"tier": "explore", "checks": {}},
                "yardstick": {"short_hold_veto": {"vetoed": vetoed, "reason": "median hold 2 sessions"}}}

    existing = Strategy(name="old", strategy_type=strategy_discovery.strategy_slug("old"), parameters={},
                        current_status="paper_trading_candidate", is_active=True)
    db.add(existing)
    db.flush()
    result = {"candidates": [row("old", True), row("new", True), row("ok", False)]}
    admission = strategy_discovery.admit_survivors(db, result)
    assert admission["admitted"] == [strategy_discovery.strategy_slug("ok")]
    assert {v["slug"]: v["enforced"] for v in admission["vetoed"]} == {
        strategy_discovery.strategy_slug("old"): False, strategy_discovery.strategy_slug("new"): True}
    db.refresh(existing)
    assert existing.current_status == "paper_trading_candidate"


def test_discovery_summary_carries_the_yardstick_with_a_context():
    frames = _frames()
    candidate = {"id": "c1", "family": "n_day_low", "universe": "single_stocks", "symbols": ["AAA"],
                 "rule": {"entry": [{"f": "at_low_5", "op": "==", "v": 1}], "exit": [{"f": "ret_1", "op": ">", "v": 0}]},
                 "stop_loss_pct": 0.1, "take_profit_pct": 0.0, "max_holding_sessions": 10}
    evaluation = strategy_discovery.evaluate_candidate(candidate, frames)
    assert {"gross_return", "entry_date", "exit_date"} <= set(evaluation["trades"].columns)
    holdout = pd.Timestamp(frames["AAA"]["date"].iloc[-1]) - pd.DateOffset(years=1)
    plain = strategy_discovery.summarize_candidate(candidate, evaluation, holdout)
    assert "yardstick" not in plain
    context = strategy_discovery.discovery_context(frames, None, irx=pd.DataFrame({"date": ["2010-01-01"],
                                                                                    "close": [1.0]}))
    scored = strategy_discovery.summarize_candidate(candidate, evaluation, holdout, context)
    score = scored["yardstick"]
    assert score["bracket"]["status"] == "ok" and set(score["bracket"]["windows"]) == {"discovery", "post_period",
                                                                                       "holdout"}
    assert score["short_hold_veto"]["status"] == "ok" and score["turnover"]["status"] == "ok"
    assert set(score["passive"]["windows"]) == {"discovery", "since_2015"}
    line = yardstick.compact(score, "discovery")
    assert set(line["net_bps"]) >= {"5bps", "10bps", "25bps", "model", "acting"}


# ------------------------------------------------------------ monthly sleeves

def test_is_monthly_sleeve_reads_the_cadence_parameter():
    assert sleeve_guardrail.is_monthly_sleeve(Strategy(parameters={"sleeve_cadence": "monthly"}))
    assert sleeve_guardrail.is_monthly_sleeve({"parameters": {"sleeve_cadence": "Monthly"}})
    assert not sleeve_guardrail.is_monthly_sleeve(Strategy(parameters={}))
    assert not sleeve_guardrail.is_monthly_sleeve(Strategy(parameters={"sleeve_cadence": "weekly"}))
    assert not sleeve_guardrail.is_monthly_sleeve(None)
    assert engine.is_monthly_sleeve is sleeve_guardrail.is_monthly_sleeve


def _days(n, active, start="2027-01-04"):
    return [{"session": d.strftime("%Y-%m-%d"), "sleeve_return": a + 0.0003, "passive_return": 0.0003}
            for d, a in zip(pd.bdate_range(start, periods=n), active)]


def test_harm_guardrail_is_the_pre_registered_rule():
    assert sleeve_guardrail.harm_guardrail([])["status"] == "no_data"
    assert sleeve_guardrail.harm_guardrail([{"session": "bad"}, "junk"])["status"] == "no_data"
    rng = np.random.default_rng(5)
    calm = sleeve_guardrail.harm_guardrail(_days(30, rng.normal(0.0, 0.002, 30)))
    assert calm["status"] == "watching" and not calm["harm"]
    ok = sleeve_guardrail.harm_guardrail(_days(80, rng.normal(0.0002, 0.002, 80)))
    assert ok["status"] == "ok"
    losing = sleeve_guardrail.harm_guardrail(_days(80, rng.normal(-0.0015, 0.002, 80)))
    assert losing["harm"] and "date-clustered t" in losing["reason"]
    # The same steady loss over fewer than 60 sessions is not yet a t-test harm...
    early = sleeve_guardrail.harm_guardrail(_days(40, rng.normal(-0.0015, 0.002, 40)))
    assert "date-clustered" not in early["reason"]
    # ...but a 15%+ active drawdown is harm at any time.
    crash = sleeve_guardrail.harm_guardrail(_days(10, [-0.03] * 10))
    assert crash["harm"] and "active drawdown" in crash["reason"]


def _sleeve_change(db, row, *, adopted_at):
    change = {"change_id": "sleeve1", "strategy_id": row.id, "strategy_type": row.strategy_type,
              "template": "market_gate", "key": "market_gate_sma", "trigger": "missed_market_move",
              "description": "no new longs while SPY is at or below its 20-day average",
              "before": {"market_gate_sma": None}, "after": {"market_gate_sma": 20},
              "adopted_at": adopted_at.isoformat(), "state": "watching",
              "recent_before": {"trades": 5, "expectancy_bps": 10.0, "cause_share": 0.5},
              "watch": {"min_trades": 20, "max_trades": 60}, "paper_only": True}
    write_audit_log(db, event_type=engine.EVENT, entity_type="strategy", entity_id=row.id,
                    action=engine.ACTION_ADOPTED, status="watching", message="x", payload=change)
    db.commit()
    return change


def test_monthly_sleeves_skip_fixed_20_and_are_judged_by_the_guardrail(db):  # noqa: F811
    row = _strategy(db, "target_weight_trend", {"sleeve_cadence": "monthly", "market_gate_sma": 20})
    now = datetime(2027, 6, 1, 22, tzinfo=UTC)
    _sleeve_change(db, row, adopted_at=now - timedelta(days=120))
    first = engine.review_changes(db, now=now)
    assert first["reviewed"] == []
    pending = first["pending"][0]
    assert pending["verdict_method"] == engine.SLEEVE_GUARDRAIL and pending["verdict_fixed_20"] == "skipped_monthly_sleeve"
    assert pending["sleeve_guardrail"]["status"] == "no_data" and pending["needed"] is None
    assert "verdict_e_process" in pending and "cost_bracket" in pending
    # The sleeve engine records daily P&L (an audit row per session); a steady loss to the arm reverts the change.
    rng = np.random.default_rng(9)
    write_audit_log(db, event_type="sleeve_engine", entity_type="strategy", entity_id=row.id, action="daily_pnl",
                    status="complete", message="x", payload={"rows": _days(80, rng.normal(-0.002, 0.002, 80))})
    db.commit()
    second = engine.review_changes(db, now=now + timedelta(days=1))
    verdict = second["reviewed"][0]
    assert verdict["verdict"] == "not_helping" and verdict["reverted"] and verdict["verdict_method"] == "sleeve_guardrail"
    db.refresh(row)
    assert "market_gate_sma" not in row.parameters and row.parameters["sleeve_cadence"] == "monthly"


def test_short_horizon_changes_keep_fixed_20(db):  # noqa: F811
    row = _strategy(db, "channel_breakout", {"market_gate_sma": 20})
    now = datetime(2027, 6, 1, 22, tzinfo=UTC)
    _sleeve_change(db, row, adopted_at=now - timedelta(days=5))
    pending = engine.review_changes(db, now=now)["pending"][0]
    assert pending["verdict_method"] == "fixed_20" and pending["verdict_fixed_20"] == "watching"
    assert set(pending["cost_bracket"]["columns"]) == {"5bps", "10bps", "25bps", "model"}


def test_sleeve_review_pauses_a_harmful_sleeve_and_reads_registered_readers(db, monkeypatch):  # noqa: F811
    harmful = _strategy(db, "sleeve_a", {"sleeve_cadence": "monthly"})
    quiet = _strategy(db, "sleeve_b", {"sleeve_cadence": "monthly"})
    _strategy(db, "rsi2_pullback")
    monkeypatch.setattr(sleeve_guardrail, "_READERS", [])

    @sleeve_guardrail.register_reader
    def reader(_db, strategy):
        if strategy.strategy_type == "sleeve_a":
            return _days(12, [-0.03] * 12)
        raise RuntimeError("no table yet")

    result = sleeve_guardrail.review_monthly_sleeves(db, now=datetime(2027, 6, 1, tzinfo=UTC))
    statuses = {r["strategy_type"]: (r["status"], r["paused"]) for r in result["reviewed"]}
    assert statuses == {"sleeve_a": ("harm", True), "sleeve_b": ("no_data", False)}
    db.refresh(harmful)
    db.refresh(quiet)
    assert harmful.current_status == "paused" and quiet.current_status == "paper_trading_active"
    assert db.query(AuditLog).filter_by(event_type="strategy_governance", action="sleeve_guardrail_pause").count() == 1


def test_guardrail_reads_the_target_weight_books_daily_pnl_rows(db):  # noqa: F811
    sleeve = _strategy(db, "tw_trend", {"sleeve_cadence": "monthly"}, status="target_weight_active")
    arm = _strategy(db, "tw_passive", {"sleeve_cadence": "monthly"}, status="target_weight_active")
    for k, day in enumerate(pd.bdate_range("2027-01-04", periods=5)):
        write_audit_log(db, event_type="target_weight_book", entity_type="target_weight_book",
                        action="sleeve_daily_pnl", status="recorded", message="x",
                        payload={"session": day.strftime("%Y-%m-%d"),
                                 "sleeves": {str(sleeve.id): {"return": 0.001 * k}, str(arm.id): {"return": 0.0005}},
                                 "passive_arm": {"strategy_id": arm.id, "return": 0.0005}})
    db.commit()
    rows = sleeve_guardrail.sleeve_daily_returns(db, sleeve)
    assert len(rows) == 5 and rows[2]["active_return"] == pytest.approx(0.0015)
    assert sleeve_guardrail.sleeve_daily_returns(db, arm) == []   # the arm is never judged against itself
    assert sleeve_guardrail.harm_guardrail(rows)["status"] == "watching"


# ------------------------------------------------------------------ probes

def test_probe_plan_spreads_the_week_and_measures_the_round_trip():
    assert cost_probes.planned_weekdays(3) == [0, 1, 3]
    assert cost_probes.planned_weekdays(5) == [0, 1, 2, 3, 4]
    assert cost_probes.planned_weekdays(0) == []
    buy = type("O", (), {"limit_price": Decimal("100")})()
    sell = type("O", (), {"limit_price": Decimal("100.10")})()
    measured = cost_probes.measure_round_trip(
        buy, sell, {"price": Decimal("100.02"), "quantity": Decimal("1"), "fee": None},
        {"price": Decimal("100.09"), "quantity": Decimal("1"), "fee": None})
    assert measured["buy_slippage_bps"] == pytest.approx(2.0)
    assert measured["sell_slippage_bps"] == pytest.approx(0.999, abs=0.01)
    assert measured["round_trip_bps"] == pytest.approx(3.0, abs=0.01) and not measured["fee_known"]
    assert cost_probes.probe_key("2027-01-04", "spy", "buy") == "cost-probe:2027-01-04:SPY:buy"
    assert cost_probes.probe_client_order_id("k").startswith("sp-probe-")


def test_probe_cycle_is_off_at_zero_and_outside_the_session(db, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "cost_probe_per_week", 0)
    assert cost_probes.run_probe_cycle(db)["status"] == "disabled"
    monkeypatch.setattr(settings, "cost_probe_per_week", 3)
    monkeypatch.setattr(settings, "active_paper_broker", "alpaca_paper")
    saturday = datetime(2026, 11, 7, 16, tzinfo=UTC)
    assert cost_probes.run_probe_cycle(db, now=saturday)["reason"] == "outside the regular session"
    status = cost_probes.probe_status(db)
    assert status["per_week"] == 3 and status["round_trips"] == 0 and status["symbols"]


def test_probe_orders_use_their_own_source_prefix_and_a_market_order():
    import tempfile
    from contextlib import ExitStack
    from pathlib import Path
    from unittest.mock import patch

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.db.base import Base
    from app.models import RiskRule
    from app.models.stock_paper import StockPaperOrder
    from app.services.paper_trade_scorecard import source_label
    from app.services.stock_paper_ledger import (PROBE_SOURCE, StockPaperError, dispatch_reserved_order,
                                                 initialize_stock_paper_account, reserve_stock_paper_order)
    from tests.test_paper_exploration import session_window_around
    from tests.test_stock_paper_ledger import FakeAlpaca

    class Gateway(FakeAlpaca):
        def __init__(self):
            super().__init__()
            self.payloads = []

        def submit_order(self, payload):
            self.payloads.append(payload)
            return {"id": "broker-1", "status": "accepted"}

    now = datetime.now(UTC)
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
        engine_ = create_engine("sqlite:///" + str(Path(tmp) / "probe.sqlite"))
        Base.metadata.create_all(engine_)
        for item in (patch.object(settings, "active_paper_broker", "alpaca_paper"),
                     patch.object(settings, "paper_broker_account_id", ""),
                     patch("app.services.stock_paper_ledger.session_bounds", return_value=session_window_around(now)),
                     patch("app.services.stock_paper_ledger.feed_status", return_value={"status": "ready"}),
                     patch("app.services.stock_paper_ledger._validate_reference_price"),
                     patch("app.services.stock_learning_cycle.evaluate_launch_admission_prerequisites",
                           return_value={"all": {"status": "pass"}})):
            stack.enter_context(item)
        with Session(engine_) as session:
            initialize_stock_paper_account(session, FakeAlpaca())
            session.add(RiskRule(name="probe", value={"max_open_positions": 10}))
            session.commit()
            with pytest.raises(StockPaperError, match="probe rotation"):
                reserve_stock_paper_order(session, symbol="ZZZZ", side="buy", quantity=Decimal("1"),
                                          reference_price=Decimal("90"), idempotency_key="p0", source=PROBE_SOURCE)
            with pytest.raises(StockPaperError, match="whole shares"):
                reserve_stock_paper_order(session, symbol="SPY", side="buy", quantity=Decimal("2"),
                                          reference_price=Decimal("90"), idempotency_key="p1", source=PROBE_SOURCE)
            key = cost_probes.probe_key(now.date().isoformat(), "SPY", "buy")
            order = reserve_stock_paper_order(session, symbol="SPY", side="buy", quantity=Decimal("1"),
                                              reference_price=Decimal("90"), idempotency_key=key, source=PROBE_SOURCE)
            assert order.client_order_id == cost_probes.probe_client_order_id(key)
            assert order.order_type == "market" and order.source == PROBE_SOURCE and order.strategy_id is None
            assert order.limit_price == Decimal("90")  # the decision price the calibration benchmarks against
            gateway = Gateway()
            submitted = dispatch_reserved_order(session, order.id, gateway=gateway)
            assert submitted.status == "accepted"
            assert gateway.payloads[0]["type"] == "market" and "limit_price" not in gateway.payloads[0]
            with pytest.raises(StockPaperError, match="no open order"):
                reserve_stock_paper_order(session, symbol="SPY", side="buy", quantity=Decimal("1"),
                                          reference_price=Decimal("90"), idempotency_key="p2", source=PROBE_SOURCE)
            assert session.query(StockPaperOrder).count() == 1
        engine_.dispose()
    assert source_label(PROBE_SOURCE) == "probe"  # never a bot lot: attribution and the loss diagnosis skip it


# --------------------------------------------------------------- dividends

def test_dividend_check_reports_booked_none_and_unavailable(db):  # noqa: F811
    assert dividend_check.dividend_status(db)["status"] == "no_data"
    rows = [{"id": "1", "activity_type": "DIV", "symbol": "SPY", "net_amount": "1.25", "date": "2026-10-31"},
            {"id": "2", "activity_type": "DIV", "symbol": "SPY", "net_amount": "1.30", "date": "2026-07-31"}]
    booked = dividend_check.record_dividend_check(db, fetch=lambda: rows)
    assert booked["status"] == "booked" and booked["entries"] == 2 and booked["net_amount"] == pytest.approx(2.55)
    assert booked["by_symbol"]["SPY"]["last"] == "2026-10-31"
    assert dividend_check.dividend_status(db)["booked"] is True
    assert dividend_check.check_dividends(db, fetch=lambda: [])["status"] == "none_found"

    def broken():
        raise RuntimeError("Alpaca paper credentials are not configured")

    unavailable = dividend_check.check_dividends(db, fetch=broken)
    assert unavailable["status"] == "unavailable" and unavailable["booked"] is None


def test_fetch_dividends_asks_the_activities_feed_for_div():
    class Client:
        def _activity_pages(self, params):
            self.params = params
            return []

    client = Client()
    dividend_check.fetch_dividends(client, now=datetime(2026, 11, 7, tzinfo=UTC))
    assert client.params["activity_types"] == "DIV" and client.params["after"] == "2025-10-03"


# ----------------------------------------------------------------- wiring

def test_endpoint_jobs_and_settings_are_wired():
    assert "/stock-paper/yardstick" in READ_PATHS
    assert required_role("GET", "/stock-paper/yardstick") == "viewer"
    routes = celery_app.conf.task_routes
    assert routes["app.tasks.jobs.yardstick_job"]["queue"] == "learning"
    for job in ("cost_probe_job", "dividend_check_job", "sleeve_guardrail_job"):
        assert routes[f"app.tasks.jobs.{job}"]["queue"] == "learning_fast"
    schedule = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}
    assert {"app.tasks.jobs.yardstick_job", "app.tasks.jobs.cost_probe_job", "app.tasks.jobs.dividend_check_job",
            "app.tasks.jobs.sleeve_guardrail_job"} <= schedule
    assert settings.cost_probe_per_week == 3 and settings.cost_probe_quantity == 1
    assert settings.yardstick_short_hold_veto is True and settings.yardstick_slot_fraction == 0.05


def test_views_render_with_an_empty_ledger(db):  # noqa: F811
    view = yardstick.yardstick_view(db)
    assert view["rules"]["bracket_bps"] == [5.0, 10.0, 25.0]
    assert view["dividends"]["status"] == "no_data"
    assert view["monthly_sleeves"]["sleeves"] == []
    assert view["probes"]["round_trips"] == 0
    from app.services.learning_scoreboard import build_learning_scoreboard

    board = build_learning_scoreboard(db)
    assert board["yardstick"]["dividends"]["status"] == "no_data"
