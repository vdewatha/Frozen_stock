"""Always-valid verdicts (verdict_stats.py): the statistics, e-LOND bookkeeping, the cap, the hurdle, shadow mode."""
from __future__ import annotations

from datetime import date, timedelta
import math

import numpy as np
import pandas as pd
import pytest

from app.core.config import settings
from app.models import AuditLog
from app.services import experiment_engine as engine
from app.services import setup_shadow as shadow
from app.services import verdict_stats as vs
from tests.test_experiment_engine import NOW, _adopt, _attribution, small_universe  # noqa: F401  (fixture)
from tests.test_loss_feedback_loop import _strategy, db  # noqa: F401  (fixture)

B = 1200.0


def _heavy(rng, n, mean, sd=200.0):
    """Student-t(3) scaled to ``sd``, symmetric, clipped inside the bound (the clip keeps the mean)."""
    return np.clip(rng.standard_t(3, size=n) / math.sqrt(3) * sd, -B + 1, B - 1) + mean


# --------------------------------------------------------------- the bets

def test_predictable_bets_follow_equation_26_and_use_only_the_past():
    x = np.array([0.5, 0.7, 0.2, 0.9])
    bets = vs.predictable_bets(x, 0.05)
    # t = 1: sigma^2_0 = 1/4, so lambda_1 = sqrt(2 log 20 / (1/4 * 1 * log 2)).
    assert bets[0] == pytest.approx(math.sqrt(2 * math.log(20) / (0.25 * math.log(2))))
    mu1 = (0.5 + 0.5) / 2
    sig1 = (0.25 + (0.5 - mu1) ** 2) / 2
    assert bets[1] == pytest.approx(math.sqrt(2 * math.log(20) / (sig1 * 2 * math.log(3))))
    # Changing x_t never changes lambda_t (predictable).
    changed = x.copy()
    changed[2] = 0.99
    assert vs.predictable_bets(changed, 0.05)[2] == pytest.approx(bets[2])
    fixed = vs.predictable_bets(x, 0.05, n_planned=120)
    assert fixed[0] == pytest.approx(math.sqrt(2 * math.log(20) / (120 * 0.25)))


def test_capital_is_one_at_the_start_and_truncated_bets_keep_it_positive():
    x = np.zeros(50)   # the worst case for a bet on "greater"
    log_k = vs.log_capital(x, 0.5, np.full(50, 100.0), "greater")
    assert log_k[0] == 0.0 and np.all(np.isfinite(log_k))
    assert log_k[1] == pytest.approx(math.log(1 - vs.BET_TRUNCATION))


# --------------------------------------------------- validity under the null

def test_betting_tests_are_valid_under_the_null_by_simulation():
    rng = np.random.default_rng(20261107)
    reps, n = 600, 300
    harm = improve = 0
    for _ in range(reps):
        values = _heavy(rng, n, 0.0)
        harm += bool((vs.betting_e_process(values, bound_bps=B, side="less") >= 1 / 0.10).any())
        improve += bool((vs.betting_e_process(values, bound_bps=B, side="greater") >= 1 / 0.05).any())
    # Ville: P(ever crossing 1/alpha) <= alpha, over the whole path, not just at n.
    assert harm / reps <= 0.10
    assert improve / reps <= 0.05


def test_composite_null_is_valid_too():
    rng = np.random.default_rng(3)
    crossed = sum(bool((vs.betting_e_process(_heavy(rng, 200, -20.0), bound_bps=B, side="greater") >= 10).any())
                  for _ in range(400))
    assert crossed / 400 <= 0.10   # a true mean below the null only lowers the drift


def test_catoni_tests_are_valid_under_the_null_with_unbounded_heavy_tails():
    rng = np.random.default_rng(11)
    reps = 500
    crossed = 0
    for _ in range(reps):
        values = rng.standard_t(3, size=300) / math.sqrt(3) * 200.0   # unbounded, variance 200^2
        crossed += bool((vs.catoni_e_process(values, sigma_bps=200.0, side="less") >= 10).any())
    assert crossed / reps <= 0.10


# --------------------------------------------------------- crossing times

def test_large_effects_cross_early_and_in_the_right_direction():
    rng = np.random.default_rng(5)
    times_bad, times_good = [], []
    for _ in range(200):
        bad = vs.betting_e_process(_heavy(rng, 400, -150.0), bound_bps=B, side="less")
        good = vs.betting_e_process(_heavy(rng, 400, 150.0), bound_bps=B, side="greater")
        times_bad.append(int(np.argmax(bad >= 10)) + 1 if (bad >= 10).any() else 10**6)
        times_good.append(int(np.argmax(good >= 20)) + 1 if (good >= 20).any() else 10**6)
    assert np.median(times_bad) < 50 and np.mean(np.array(times_bad) <= 120) > 0.95
    assert np.median(times_good) < 60 and np.mean(np.array(times_good) <= 120) > 0.95
    # The wrong-way test does not fire on a strong effect.
    assert not (vs.betting_e_process(_heavy(rng, 400, 150.0), bound_bps=B, side="less") >= 10).any()


def test_catoni_crosses_for_a_large_effect():
    rng = np.random.default_rng(9)
    e = vs.catoni_e_process(rng.standard_t(3, size=300) / math.sqrt(3) * 200.0 - 150.0, sigma_bps=200.0, side="less")
    assert (e >= 10).any() and int(np.argmax(e >= 10)) < 120


# ------------------------------------------------------------ the cap rule

def _clusters(values, per_day=1):
    out = []
    for i, value in enumerate(values):
        day = (date(2026, 1, 1) + timedelta(days=i // per_day)).isoformat()
        out.append(vs.Observation(float(value), day, day))
    return vs.cluster_by_entry_date(out)


def test_cap_keeps_a_change_whose_lower_bound_clears_minus_10():
    rng = np.random.default_rng(1)
    values = 40.0 + rng.normal(0, 2.0, size=130)   # the bound sits ~log(10) / (120 x 2c) x 2B below the mean
    out = vs.e_process_verdict(_clusters(values), bound_bps=B, sigma_bps=300.0, improvement_level=1e-9)
    assert out["verdict"] == "inconclusive" and out["kept_at_cap"] and out["decided_at_trades"] == vs.CAP_TRADES
    assert out["lcb_bps"] > vs.KEEP_LCB_BPS


def test_cap_reverts_when_the_lower_bound_is_below_minus_10():
    rng = np.random.default_rng(2)
    values = -30.0 + rng.normal(0, 2.0, size=130)
    out = vs.e_process_verdict(_clusters(values), bound_bps=B, sigma_bps=300.0, improvement_level=1e-9,
                               harm_alpha=1e-12)   # harm test switched off so the cap decides
    assert out["verdict"] == "not_helping" and out["kept_at_cap"] is False and out["lcb_bps"] <= vs.KEEP_LCB_BPS


def test_harm_is_checked_before_improvement_and_before_the_cap():
    out = vs.e_process_verdict(_clusters([-400.0] * 40), bound_bps=B, sigma_bps=300.0, improvement_level=0.05)
    assert out["verdict"] == "not_helping" and out["reason"] == "harm test crossed" and out["decided_at_trades"] < 40
    watching = vs.e_process_verdict(_clusters([0.0] * 30), bound_bps=B, sigma_bps=300.0, improvement_level=0.05)
    assert watching["verdict"] == "watching" and watching["trades"] == 30


def test_lower_bound_sits_below_the_mean_and_tightens_with_data():
    rng = np.random.default_rng(4)
    values = _heavy(rng, 400, 0.0)
    short = vs.lower_confidence_bound_bps(values[:60], bound_bps=B, n_planned=60)
    long = vs.lower_confidence_bound_bps(values, bound_bps=B, n_planned=400)
    assert short < values[:60].mean() and long < values.mean()
    assert values.mean() - long < values[:60].mean() - short


def test_a_trade_beyond_the_bound_raises_the_alarm_and_switches_to_catoni():
    values = [10.0] * 20 + [-(B + 500)] + [10.0] * 10
    out = vs.e_process_verdict(_clusters(values), bound_bps=B, sigma_bps=400.0, improvement_level=1e-9)
    assert out["bound_breach"] and out["breach_at_observation"] == 21 and out["process"] == "catoni"


def test_same_date_trades_are_one_observation_ordered_by_last_close():
    obs = [vs.Observation(100.0, "2026-01-02", "2026-01-09"), vs.Observation(-50.0, "2026-01-02", "2026-01-05"),
           vs.Observation(30.0, "2026-01-03", "2026-01-06")]
    clusters = vs.cluster_by_entry_date(obs)
    assert [c.entry_date for c in clusters] == ["2026-01-03", "2026-01-02"]
    assert clusters[1].value_bps == pytest.approx(25.0) and clusters[1].trades == 2
    assert vs.effective_trades(clusters, 0.5) == pytest.approx(1 + 2 / 1.5)


# ------------------------------------------------------------------ e-LOND

def test_elond_levels_follow_the_discount_sequence_and_the_discovery_count():
    assert vs.elond_gamma(1) == 0.5 and vs.elond_gamma(3) == pytest.approx(1 / 12)
    assert sum(vs.elond_gamma(j) for j in range(1, 10_000)) == pytest.approx(1.0, abs=1e-3)
    assert vs.elond_level(1, 0, 0.10) == pytest.approx(0.05)
    assert vs.elond_level(3, 1, 0.10) == pytest.approx(0.10 / 12 * 2)
    with pytest.raises(ValueError):
        vs.elond_gamma(0)


def test_elond_bookkeeping_assigns_indices_in_adoption_order_and_counts_earlier_discoveries():
    state = {"alpha": 0.10, "changes": {}}
    changes = [{"change_id": "b", "adopted_at": "2026-02-01"}, {"change_id": "a", "adopted_at": "2026-01-01"},
               {"change_id": "c", "adopted_at": "2026-03-01"}]
    vs.assign_indices(state, changes)
    assert {k: v["index"] for k, v in state["changes"].items()} == {"a": 1, "b": 2, "c": 3}
    vs.assign_indices(state, changes + [{"change_id": "d", "adopted_at": "2025-01-01"}])
    assert state["changes"]["d"]["index"] == 4   # a late-seen change never moves the others
    state["changes"]["c"]["discovered"] = True
    assert vs.level_for(state, "b") == (2, 0, pytest.approx(0.10 / 6))
    assert vs.level_for(state, "d") == (4, 1, pytest.approx(0.10 / 20 * 2))   # c (index 3) counts for d only
    entry = {"levels": [[2, 0.01], [5, 0.02]]}
    clusters = _clusters([1.0] * 7)
    assert vs.levels_by_observation(entry, clusters, 0.03) == [0.01, 0.01, 0.02, 0.02, 0.02, 0.03, 0.03]


def test_elond_controls_false_confirmations_over_a_stream_of_null_changes():
    rng = np.random.default_rng(13)
    confirmations, streams = 0, 60
    for _ in range(streams):
        discoveries = 0
        for j in range(1, 11):
            level = vs.elond_level(j, discoveries)
            out = vs.e_process_verdict(_clusters(_heavy(rng, 150, 0.0)), bound_bps=B, sigma_bps=300.0,
                                       improvement_level=level, harm_alpha=1e-12)
            discoveries += out["verdict"] == "working"
        confirmations += discoveries > 0
    assert confirmations / streams <= 0.10   # all nulls: FDR = P(any false confirmation)


# ------------------------------------------------------------ the hurdle

def _trades(rows):
    return pd.DataFrame(rows, columns=["symbol", "decision_date", "entry_date", "exit_date", "net_return"])


def test_paired_backtest_is_candidate_minus_baseline_over_the_union():
    base = _trades([("A", "2026-01-02", "2026-01-05", "2026-01-09", 0.02),
                    ("B", "2026-01-02", "2026-01-05", "2026-01-09", -0.03)])
    cand = _trades([("A", "2026-01-02", "2026-01-05", "2026-01-09", 0.02),     # same trade: 0
                    ("C", "2026-01-06", "2026-01-07", "2026-01-12", 0.01)])    # only the candidate: +100
    frame = vs.paired_backtest(base, cand, adjust=False)
    assert sorted(frame["value_bps"].round(6).tolist()) == [0.0, 100.0, 300.0]   # B blocked: +300
    stats = vs.paired_stats(frame)
    assert stats["decisions"] == 3 and stats["mean_bps"] == pytest.approx(400 / 3, abs=1e-3)
    assert stats["clusters"] == 2 and stats["nonzero_share"] == pytest.approx(2 / 3, abs=1e-4)


def _stats(mean, std, n, clusters=None, rng_seed=0):
    rng = np.random.default_rng(rng_seed)
    values = rng.normal(mean, std, size=n)
    values = (values - values.mean()) / values.std(ddof=1) * std + mean
    days = [f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(clusters or n)]
    entries = [days[i % len(days)] for i in range(n)]
    return vs.paired_stats(pd.DataFrame({"value_bps": values, "entry_date": entries}))


def test_hurdle_passes_on_t_at_least_3_on_the_paired_gain():
    strong = _stats(20.0, 100.0, 400)          # t = 20 / (100 / 20) = 4
    assert strong["t_stat"] == pytest.approx(4.0, rel=0.05)
    hurdle = vs.multiplicity_hurdle(strong, trials=40, trial_sharpes=[0.2, 0.0, 0.05], label="x")
    assert hurdle["passed"] and hurdle["basis"] == "paired_difference" and hurdle["trials"] == 40
    weak = _stats(8.0, 100.0, 400)             # t = 1.6
    assert not vs.multiplicity_hurdle(weak, trials=40, trial_sharpes=[0.08, 0.0, 0.05, -0.03])["passed"]


def test_same_date_trades_count_once_in_the_t_stat():
    rng = np.random.default_rng(8)
    per_day = rng.normal(0.0, 100.0, size=40)
    per_day = per_day - per_day.mean() + 40.0
    days = [f"2026-01-{i + 1:02d}" if i < 28 else f"2026-02-{i - 27:02d}" for i in range(40)]
    one_each = vs.paired_stats(pd.DataFrame({"value_bps": per_day, "entry_date": days}))
    ten_each = vs.paired_stats(pd.DataFrame({"value_bps": np.repeat(per_day, 10), "entry_date": np.repeat(days, 10)}))
    # Ten identical trades per date carry one date's information: the clustered t does not grow with them.
    naive = ten_each["mean_bps"] / (ten_each["std_bps"] / math.sqrt(400))
    assert ten_each["t_stat"] == pytest.approx(one_each["t_stat"], rel=1e-3)
    assert naive > 3 * ten_each["t_stat"]


def test_hurdle_passes_on_the_deflated_sharpe_and_more_trials_make_it_harder():
    stats = {"decisions": 3000, "sharpe": 0.06, "skewness": 0.0, "kurtosis": 3.0, "t_stat": 2.0, "mean_bps": 6.0}
    few = vs.multiplicity_hurdle(stats, trials=2, trial_sharpes=[0.06, 0.0])
    many = vs.multiplicity_hurdle(stats, trials=400, trial_sharpes=[0.06, 0.0, 0.03, -0.02])
    assert few["deflated_sharpe"] > many["deflated_sharpe"]
    assert few["passed"] and not many["passed"]
    assert vs.multiplicity_hurdle({}, trials=5)["passed"] is None   # no paired data: unavailable, not a fail
    same = vs.paired_stats(pd.DataFrame({"value_bps": [0.0] * 50, "entry_date": ["2026-01-02"] * 50}))
    assert vs.multiplicity_hurdle(same, trials=5)["passed"] is False   # a no-op candidate never clears it


def test_hurdle_in_the_adoption_path_is_reported_and_acts_only_when_on(monkeypatch):
    paired = {"sma20": {"decisions": 400, "sharpe": 0.05, "skewness": 0.0, "kurtosis": 3.0, "t_stat": 1.0,
                        "mean_bps": 5.0}}
    decision = {"adopted": "sma20", "eligible": {"sma20": 7.0}}
    monkeypatch.setattr(settings, "experiment_multiplicity_hurdle", False)
    assert engine.apply_multiplicity_hurdle(decision, paired, prior_trials=10, tried_now=1) == "sma20"
    assert decision["multiplicity_hurdle"]["passed"] is False and decision["multiplicity_hurdle"]["trials"] == 11
    assert "blocked_by_hurdle" not in decision
    monkeypatch.setattr(settings, "experiment_multiplicity_hurdle", True)
    decision = {"adopted": "sma20", "eligible": {"sma20": 7.0}}
    assert engine.apply_multiplicity_hurdle(decision, paired, prior_trials=10, tried_now=1) is None
    assert decision["adopted"] == "sma20" and decision["blocked_by_hurdle"] == "sma20"
    # Without paired data (book experiments) the hurdle is reported unavailable and never blocks.
    decision = {"adopted": "cap2", "eligible": {"cap2": 3.0}}
    assert engine.apply_multiplicity_hurdle(decision, {}, prior_trials=0, tried_now=2) == "cap2"
    assert decision["multiplicity_hurdle"]["passed"] is None


def test_prior_trials_count_every_candidate_ever_tried_per_strategy():
    history = [{"strategy_type": "macd_momentum", "setups": {"baseline": {}, "a": {}, "b": {}},
                "paired_moments": {"a": {"sharpe": 0.2}, "b": {}}},
               {"strategy_type": "macd_momentum", "setups": {"baseline": {}, "c": {}}},
               {"strategy_type": "rsi2_pullback", "setups": {"baseline": {}, "d": {}}}]
    prior = engine.prior_trials_by_strategy(history)
    assert prior["macd_momentum"] == {"trials": 3, "sharpes": [0.2]} and prior["rsi2_pullback"]["trials"] == 1


# -------------------------------------------------------- shadow mode

def _scored(symbol, session, net, *, off, sma20, entry=None, exit_=None):
    entry = entry or session
    return {"symbol": symbol, "strategy": "channel_breakout", "session": session, "status": "scored",
            "net_return": net, "entry_date": entry, "exit_date": exit_ or entry, "barrier": "vertical",
            "templates": {"market_gate": {"live": "sma20", "live_allowed": sma20, "candidates": {"off": off,
                                                                                               "sma20": sma20}}}}


def _report(decisions):
    return {"as_of": "2026-12-31", "decisions": decisions, "records": [], "totals": {}, "window": {}, "min_trades": 4}


def _harmful_shadow(adopted_session, n):
    """``n`` decisions after the change where the old value (off) took +5% winners the adopted sma20 blocked."""
    start = date.fromisoformat(adopted_session)
    return [_scored(f"S{i}", (start + timedelta(days=i + 1)).isoformat(), 0.05, off=True, sma20=False)
            for i in range(n)]


def test_shadow_mode_records_the_e_process_verdict_beside_the_acting_one(db, monkeypatch, small_universe):  # noqa: F811
    monkeypatch.setattr(settings, "experiment_verdict_min_trades", 5)
    row = _strategy(db, "channel_breakout")
    change = _adopt(db, row, monkeypatch, small_universe)
    assert change["template"] == "market_gate"
    monkeypatch.setattr(engine, "_spy_frame", lambda db: None)   # market gate: raw differences, no SPY needed
    session = shadow.gate_cost.last_completed_session(NOW).isoformat()
    monkeypatch.setattr(shadow, "latest_scores", lambda db: _report(_harmful_shadow(session, 3)))
    review = engine.review_changes(db, now=NOW + timedelta(days=10))
    pending = review["pending"][0]
    assert pending["verdict_method"] == "fixed_20" and pending["verdict_fixed_20"] == "watching"
    assert pending["verdict_e_process"]["source"] == "shadow_paired" and pending["verdict_e_process"]["trades"] == 3
    assert pending["verdict_e_process"]["adjusted_for_spy"] is False
    state = db.query(AuditLog).filter(AuditLog.event_type == vs.EVENT, AuditLog.action == vs.ACTION_ELOND).one()
    assert state.payload["changes"][change["change_id"]]["index"] == 1
    assert state.payload["changes"][change["change_id"]]["levels"] == [[3, pytest.approx(0.05)]]

    # Five live trades: the fixed rule acts (working); the e-process verdict rides along.
    for i in range(5):
        _attribution(db, strategy_type="channel_breakout", cause="edge", net_return=0.02,
                     closed_at=NOW + timedelta(days=2 + i), opened_at=NOW + timedelta(days=1 + i))
    monkeypatch.setattr(shadow, "latest_scores", lambda db: _report(_harmful_shadow(session, 4)))
    review = engine.review_changes(db, now=NOW + timedelta(days=11))
    verdict = review["reviewed"][0]
    assert verdict["verdict"] == "working" and verdict["verdict_method"] == "fixed_20"
    assert verdict["verdict_fixed_20"] == "working" and verdict["verdict_e_process"]["method"] == "e_process"
    assert verdict["verdict_e_process"]["verdict"] in ("watching", "not_helping")
    stored = db.query(AuditLog).filter(AuditLog.action == engine.ACTION_VERDICT).one().payload
    assert "verdict_e_process" in stored and stored["verdict_method_setting"] == "fixed_20"
    listed = engine.adopted_changes(db)[0]
    assert listed["verdict_method"] == "fixed_20" and listed["verdict_e_process"]["method"] == "e_process"
    ledger = engine.experiment_ledger(db)
    assert ledger["verdict_methods"]["acting"] == "fixed_20" and ledger["verdict_methods"]["e_lond"]["changes"] == 1
    from app.services.engine_audit import build_engine_audit

    record = build_engine_audit(db, now=NOW + timedelta(days=12))["changes"][0]
    assert record["verdict_fixed_20"] == "working" and record["verdict_e_process"] in ("watching", "not_helping")


def test_setting_e_process_makes_the_e_process_judge_act(db, monkeypatch, small_universe):  # noqa: F811
    monkeypatch.setattr(settings, "experiment_verdict_min_trades", 40)   # shadow early revert needs 40
    row = _strategy(db, "channel_breakout")
    change = _adopt(db, row, monkeypatch, small_universe)
    monkeypatch.setattr(engine, "_spy_frame", lambda db: None)
    monkeypatch.setattr(settings, "experiment_verdict_method", "e_process")
    session = shadow.gate_cost.last_completed_session(NOW).isoformat()
    monkeypatch.setattr(shadow, "latest_scores", lambda db: _report(_harmful_shadow(session, 30)))
    review = engine.review_changes(db, now=NOW + timedelta(days=40))
    verdict = review["reviewed"][0]
    # No live trades at all, so the fixed rule is still watching; the e-process harm test reverts.
    assert verdict["verdict"] == "not_helping" and verdict["reverted"] and verdict["verdict_method"] == "e_process"
    assert verdict["verdict_fixed_20"] == "watching" and verdict["verdict_e_process"]["verdict"] == "not_helping"
    assert verdict["shadow_verdict"] is False and verdict["reason"].startswith("e-process: harm test crossed")
    db.refresh(row)
    assert not row.parameters.get("market_gate_sma")
    state = vs.elond_state(db)
    assert state["changes"][change["change_id"]]["decision"]["verdict"] == "not_helping"


def test_e_process_setting_falls_back_to_the_fixed_rule_when_its_data_is_unreadable(db, monkeypatch, small_universe):  # noqa: F811
    monkeypatch.setattr(settings, "experiment_verdict_min_trades", 5)
    row = _strategy(db, "channel_breakout")
    _adopt(db, row, monkeypatch, small_universe)
    monkeypatch.setattr(settings, "experiment_verdict_method", "e_process")

    def boom(*args, **kwargs):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(engine, "e_process_judgement", boom)
    for i in range(5):
        _attribution(db, strategy_type="channel_breakout", cause="edge", net_return=0.02,
                     closed_at=NOW + timedelta(days=2 + i), opened_at=NOW + timedelta(days=1 + i))
    verdict = engine.review_changes(db, now=NOW + timedelta(days=9))["reviewed"][0]
    assert verdict["verdict"] == "working" and verdict["verdict_method"] == "fixed_20"
    assert verdict["verdict_method_setting"] == "e_process" and verdict["verdict_e_process"]["status"] == "unavailable"


def test_measured_inputs_row_overrides_the_constants(db):  # noqa: F811
    assert vs.current_inputs(db)["recorded"] is False
    vs.record_inputs(db, {"as_of": "2026-11-07", "source": "ledger", "rho": 0.2,
                          "loss_bound_bps": {"technical": 900.0, "reversion": 1800.0},
                          "sigma_bps": {"technical": 300.0, "reversion": 500.0}})
    inputs = vs.current_inputs(db)
    assert inputs["recorded"] and inputs["rho"] == 0.2 and inputs["loss_bound_bps"]["technical"] == 900.0


def test_paired_observations_adjust_for_spy_and_skip_decisions_neither_value_takes():
    spy = pd.DataFrame({"date": ["2026-01-02", "2026-01-05"], "open": [100.0, 101.0], "close": [100.5, 102.0]})
    decisions = [
        {**_scored("A", "2026-01-01", 0.03, off=True, sma20=False, entry="2026-01-02", exit_="2026-01-05"),
         "barrier": "take_profit"},
        _scored("B", "2026-01-01", 0.01, off=True, sma20=True, entry="2026-01-02", exit_="2026-01-05"),
        _scored("C", "2026-01-01", 0.01, off=False, sma20=False, entry="2026-01-02", exit_="2026-01-05"),
    ]
    raw = vs.paired_observations(decisions, "market_gate", "off", adjust=False)
    assert [o.value_bps for o in raw] == [pytest.approx(-300.0), 0.0]
    adjusted = vs.paired_observations(decisions, "market_gate", "off", spy=spy, adjust=True)
    assert adjusted[0].value_bps == pytest.approx(-(0.03 - 0.02) * 10_000)   # SPY open 100 -> close 102
    assert not vs.adjusts_for_spy("market_gate") and vs.adjusts_for_spy("earnings_blackout")
