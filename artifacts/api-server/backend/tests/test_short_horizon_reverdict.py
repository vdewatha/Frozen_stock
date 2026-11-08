"""Step 4 re-verdict: the pre-registered decision functions, the sleeve arithmetic, and the one-time seed step."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.models import AuditLog, Strategy
from app.services import seed
from tests.test_loss_feedback_loop import db  # noqa: F401  (fixture)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_short_horizon_reverdict as rv  # noqa: E402

PASS = {"2008-07+": {"passes": True}, "2015+": {"passes": True}}
FAIL_ONE = {"2008-07+": {"passes": True}, "2015+": {"passes": False}}


# ------------------------------------------------------------------ decision rule

def test_required_gross_is_the_larger_of_30_bps_and_three_times_cost():
    assert rv.required_gross_bps(5) == 30.0
    assert rv.required_gross_bps(10) == 30.0
    assert rv.required_gross_bps(25) == 75.0
    assert rv.required_gross_bps(27.0) == pytest.approx(81.0)


def test_cost_checks_need_all_three():
    ok = rv.cost_checks(46.4, 10.0, 1.2, PASS)
    assert ok["passes"] and ok["haircut_sharpe"] == 0.6
    # the IBS reading at 10 bps: gross and marginal pass, Sharpe 0.667 is 0.33 after the haircut
    ibs = rv.cost_checks(46.4, 10.0, 0.667, PASS)
    assert ibs["gross_ok"] and ibs["marginal_ok"] and not ibs["sharpe_ok"] and not ibs["passes"]
    assert not rv.cost_checks(29.9, 10.0, 1.2, PASS)["gross_ok"]          # 29.9 < 30
    assert not rv.cost_checks(46.4, 10.0, 1.2, FAIL_ONE)["passes"]        # one window fails
    assert not rv.cost_checks(46.4, 10.0, None, PASS)["passes"]
    assert not rv.cost_checks(46.4, 10.0, 1.2, {})["passes"]


def test_variant_also_needs_post_2021_gross():
    assert rv.cost_checks(40.0, 10.0, 1.2, PASS, post_gross_bps=31.0, variant=True)["passes"]
    out = rv.cost_checks(40.0, 10.0, 1.2, PASS, post_gross_bps=3.9, variant=True)
    assert not out["post_2021_gross_ok"] and not out["passes"]


def _checks(**passes):
    return {cost: {"passes": passes.get(cost.replace("bps", ""), False)} for cost in rv.COSTS}


def test_outcome_branches():
    assert rv.outcome({"10bps": {"passes": True}, "25bps": {"passes": True}, "model": {"passes": True}})["decision"] \
        == "sleeve_20"
    # 25 bps alone is not enough: the model must hold too
    at_25 = {"10bps": {"passes": True}, "25bps": {"passes": True}, "model": {"passes": False}}
    pending = rv.outcome(at_25)
    assert pending["decision"] == "keep_paper_size_pending_cost" and "10 bps" in pending["pending"]
    assert rv.outcome(at_25, measured_cost_bps=12.0)["decision"] == "keep_paper_size_pending_cost"
    assert rv.outcome(at_25, measured_cost_bps=9.5)["decision"] == "sleeve_10"
    none = {"10bps": {"passes": False}, "25bps": {"passes": False}, "model": {"passes": False}}
    assert rv.outcome(none)["decision"] == "shadow"
    assert rv.outcome(none, variant=True)["decision"] == "dropped"
    assert rv.outcome(none, stock_leg=True)["decision"] == "stays_shadow"
    # a pass at 25 bps and the model but not at 10 bps still takes the 20% branch
    assert rv.outcome({"10bps": {"passes": False}, "25bps": {"passes": True},
                       "model": {"passes": True}})["decision"] == "sleeve_20"


# ------------------------------------------------------------------ series arithmetic

def _days(n, start="2015-01-02"):
    return pd.bdate_range(start, periods=n)


def test_blend_rebalances_monthly_and_matches_constant_returns():
    idx = _days(60)
    a = pd.Series(0.001, index=idx)
    b = pd.Series(0.0, index=idx)
    mix = rv.blend({"a": a, "b": b}, {"a": 0.2, "b": 0.8}, idx[0])
    # day one is exactly the target mix; weights drift up within the month and reset after its last session
    assert mix.iloc[0] == pytest.approx(0.0002)
    month = idx.to_period("M")
    first_of_second = int(np.flatnonzero(month != month[0])[0])
    assert mix.iloc[first_of_second] == pytest.approx(0.0002)
    assert mix.iloc[first_of_second - 1] > 0.0002


def test_series_stats_sharpe_is_over_bills_and_drawdown_positive():
    idx = _days(504)
    rng = np.random.default_rng(1)
    r = pd.Series(rng.normal(0.0005, 0.01, len(idx)), index=idx)
    bills = pd.Series(0.0001, index=idx)
    stats = rv.series_stats(r, bills)
    excess = r - bills
    assert stats["sharpe"] == pytest.approx(excess.mean() / excess.std(ddof=1) * np.sqrt(252), rel=1e-3)
    assert 0 < stats["max_drawdown"] < 1


def test_marginal_check_passes_only_with_higher_sharpe_and_small_extra_drawdown():
    idx = _days(800, "2008-01-02")
    rng = np.random.default_rng(2)
    spy = pd.Series(rng.normal(0.0004, 0.012, len(idx)), index=idx)
    bills = pd.Series(0.00005, index=idx)
    good = pd.Series(0.0004 + rng.normal(0, 0.002, len(idx)), index=idx)      # high Sharpe, low risk
    bad = pd.Series(rng.normal(-0.0005, 0.03, len(idx)), index=idx)           # loses, volatile
    assert rv.marginal_check(good, spy, bills, equity_weight=0.2, slice_=0.2, start=idx[0])["passes"]
    out = rv.marginal_check(bad, spy, bills, equity_weight=0.2, slice_=0.2, start=idx[0])
    assert not out["passes"] and out["sharpe_delta"] < 0


def _frame(closes, opens=None, start="2015-01-02"):
    idx = _days(len(closes), start)
    closes = np.asarray(closes, float)
    opens = closes if opens is None else np.asarray(opens, float)
    return pd.DataFrame({"date": idx.strftime("%Y-%m-%d"), "open": opens, "high": np.maximum(opens, closes),
                         "low": np.minimum(opens, closes), "close": closes, "volume": 1e6})


def test_slot_returns_marks_a_trade_and_earns_bills_when_flat():
    frame = _frame([100, 100, 101, 103, 103, 103], opens=[100, 100, 100.5, 102, 104, 103])
    bills = pd.Series(0.0001, index=pd.DatetimeIndex(pd.to_datetime(frame["date"])))
    trade = pd.DataFrame([{"entry_idx": 2, "exit_idx": 4, "entry_raw": 100.5, "exit_raw": 104.0,
                           "entry_at": "open", "exit_at": "open"}])
    out = rv.slot_returns(trade, frame, np.array([0.0]), bills).to_numpy()
    assert out[0] == pytest.approx(0.0001) and out[1] == pytest.approx(0.0001)
    assert out[2] == pytest.approx(101 / 100.5 - 1)
    assert out[3] == pytest.approx(103 / 101 - 1)
    assert out[4] == pytest.approx(104 / 103 * 1.0001 - 1)      # open exit, then T-bills for the session
    assert np.prod(1 + out[2:5]) == pytest.approx(104 / 100.5 * 1.0001)
    charged = rv.slot_returns(trade, frame, np.array([0.002]), bills).to_numpy()
    assert np.prod(1 + charged[2:5]) == pytest.approx(104 / 100.5 * 1.0001 * 0.999 ** 2)


def test_variant_buys_the_close_after_a_volatility_scaled_down_day(monkeypatch):
    rng = np.random.default_rng(4)
    closes = 100 * np.cumprod(1 + rng.normal(0, 0.005, 60))
    closes[40] = closes[39] * 0.96          # an eight-sigma down day
    opens = closes.copy()
    opens[41] = closes[40] * 1.01
    frames = {s: _frame(closes, opens) for s in rv.INDEX}
    trades = rv.variant_trades(frames)
    day = trades.loc[trades["symbol"] == "SPY"]
    row = day.loc[day["entry_idx"] == 40].iloc[0]
    assert row["exit_idx"] == 41 and row["entry_at"] == "close" and row["exit_at"] == "open"
    assert row["gross_return"] == pytest.approx(0.01)
    assert (day["z"] <= rv.VARIANT_Z).all()


def test_liquid_bars_drops_flat_zero_volume_prints():
    frame = _frame(np.full(40, 50.0))
    frame.loc[30:, "volume"] = 0.0
    ok = rv.liquid_bars(frame)
    assert not ok[:19].any()            # no 20-session median yet
    assert ok[19:29].all()
    assert not ok[29:].any()            # tomorrow (bar 30) does not trade, and nothing after does


def test_shared_slots_take_first_come_and_free_on_exit():
    rows = pd.DataFrame({"symbol": ["A", "B", "C", "D"],
                         "entry_date": ["2020-01-02", "2020-01-02", "2020-01-03", "2020-01-06"],
                         "exit_date": ["2020-01-06", "2020-01-08", "2020-01-07", "2020-01-07"]})
    taken = rv.shared_slot_accept(rows, 2)
    assert list(taken["symbol"]) == ["A", "B", "D"]     # C finds both slots full; A's open exit frees one for D


# ------------------------------------------------------------------ seed step

def _seed_rows(db):  # noqa: F811
    seed.seed_defaults(db)
    return {row.strategy_type: row for row in db.query(Strategy).all()}


def _events(db, row):  # noqa: F811
    return db.query(AuditLog).filter(AuditLog.action == seed.SHORT_HORIZON_ACTION,
                                     AuditLog.entity_id == row.id).all()


def test_verdicts_are_the_reports_shadow_outcomes():
    assert {k: v["decision"] for k, v in seed.SHORT_HORIZON_VERDICTS.items()} == {
        "ibs_mean_reversion": "shadow", "rsi2_pullback": "shadow"}


def test_seed_moves_ibs_and_rsi2_to_shadow_once_and_never_overrides(db):  # noqa: F811
    rows = _seed_rows(db)
    for slug in ("ibs_mean_reversion", "rsi2_pullback"):
        assert rows[slug].current_status == "shadow", slug
        (event,) = _events(db, rows[slug])
        assert event.status == "complete" and event.event_type == "strategy_governance"
        assert event.payload["new_status"] == "shadow" and event.payload["in_sample"] is True
        assert event.payload["source"] == seed.SHORT_HORIZON_REPORT
    # RSI(2)'s stock leg keeps its shadow_symbols; its scope is unchanged
    assert "AAPL" in rows["rsi2_pullback"].parameters["shadow_symbols"]
    # An operator brings IBS back: later seed passes never shadow it again.
    rows["ibs_mean_reversion"].current_status = "paper_trading_candidate"
    db.commit()
    rows = _seed_rows(db)
    assert rows["ibs_mean_reversion"].current_status == "paper_trading_candidate"
    assert len(_events(db, rows["ibs_mean_reversion"])) == 1
    assert seed.apply_short_horizon_reverdict(db) == []


def test_seed_records_a_skip_for_a_paused_row(db):  # noqa: F811
    db.add(Strategy(name="IBS Mean Reversion", strategy_type="ibs_mean_reversion", parameters={},
                    current_status="paused"))
    db.commit()
    assert seed.apply_short_horizon_reverdict(db) == []
    row = db.query(Strategy).filter_by(strategy_type="ibs_mean_reversion").one()
    assert row.current_status == "paused"
    (event,) = _events(db, row)
    assert event.status == "skipped"
    # the skip is the decision: a later re-activation is not shadowed
    row.current_status = "paper_trading_active"
    db.commit()
    assert seed.apply_short_horizon_reverdict(db) == []
    assert row.current_status == "paper_trading_active"


def test_pending_decision_keeps_the_row_and_records_the_condition(db):  # noqa: F811
    db.add(Strategy(name="IBS Mean Reversion", strategy_type="ibs_mean_reversion", parameters={},
                    current_status="paper_trading_candidate"))
    db.commit()
    verdicts = {"ibs_mean_reversion": {"decision": "keep_paper_size_pending_cost"}}
    assert seed.apply_short_horizon_reverdict(db, verdicts) == []
    row = db.query(Strategy).filter_by(strategy_type="ibs_mean_reversion").one()
    assert row.current_status == "paper_trading_candidate"
    (event,) = _events(db, row)
    assert event.status == "pending" and "10 bps" in event.payload["pending"]
    assert seed.apply_short_horizon_reverdict(db, verdicts) == []
    assert len(_events(db, row)) == 1


def test_unknown_decision_is_refused(db):  # noqa: F811
    db.add(Strategy(name="IBS Mean Reversion", strategy_type="ibs_mean_reversion", parameters={},
                    current_status="paper_trading_candidate"))
    db.commit()
    with pytest.raises(ValueError):
        seed.apply_short_horizon_reverdict(db, {"ibs_mean_reversion": {"decision": "sleeve_20"}})
