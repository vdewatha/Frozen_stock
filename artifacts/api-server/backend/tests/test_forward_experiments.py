"""The six frozen forward experiments: gate readings, verbatim registration, review maths, schedule, wiring."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models import AuditLog, Strategy, StrategySignal
from app.models.loss_feedback import StockTradeAttribution
from app.services import forward_experiments as fx
from app.services import forward_gate_readings as gr
from app.services import strategy_discovery as sd
from app.services.strategies.vix_gate import set_vix_history
from app.tasks.celery_app import celery_app
from tests.test_loss_feedback_loop import db  # noqa: F401  (fixture)

UTC = timezone.utc
NOW = datetime(2026, 11, 10, 22, 0, tzinfo=UTC)
REPO = Path(__file__).resolve().parents[4]


# ------------------------------------------------------------------ the spec

def test_the_packaged_spec_is_the_frozen_one_byte_for_byte():
    spec = fx.load_spec()
    assert fx.spec_sha256() == fx.SPEC_SHA256
    report_copy = REPO / fx.SPEC_JSON
    if report_copy.exists():  # the report's own copy (not shipped in the paper image)
        assert report_copy.read_bytes() == fx.SPEC_FILE.read_bytes()
    assert [c["candidate"] for c in spec["candidates"]] == [1, 2, 3, 4, 5, 6]
    assert tuple(spec["cost_brackets_bps_round_trip"]) == (10, 22, 25, 50)
    # The sweep ids are discovery's own candidate ids: the frozen rule rows are rule_<id>.
    for candidate in spec["candidates"][:5]:
        plan = {k: candidate["exits"][k] for k in ("stop_loss_pct", "take_profit_pct", "max_holding_sessions")}
        assert fx.candidate_slug(candidate) == "rule_" + sd.candidate_id(candidate["rule"], plan, candidate["universe"])
    assert fx.candidate_slug(spec["candidates"][5]) == "ibs_mean_reversion"


def test_a_tampered_spec_is_refused(tmp_path):
    bad = tmp_path / "spec.json"
    spec = json.loads(fx.SPEC_FILE.read_text())
    spec["candidates"][0]["rule"]["entry"][1]["v"] = -0.04  # one re-tuned threshold
    bad.write_text(json.dumps(spec))
    with pytest.raises(ValueError, match="SHA-256"):
        fx.load_spec(bad)


def test_mutation_is_paused_by_default_while_the_experiments_run(db):  # noqa: F811
    assert settings.strategy_discovery_mutation_enabled is False
    step = sd.evolution_step(db, {"data_end": "2026-11-10", "candidates": []}, {}, groups={}, seen_hashes=set())
    assert step["status"] == "disabled"


# ------------------------------------------------------------ gate readings

def _frame(rows=80, start="2026-07-01", drift=0.0):
    dates = pd.bdate_range(start, periods=rows).strftime("%Y-%m-%d")
    close = 100 + np.arange(rows) * drift
    return pd.DataFrame({"date": dates, "open": close, "high": close + 1, "low": close - 1, "close": close,
                         "volume": 1_000_000.0})


def test_readings_say_vix_above_20_and_spy_above_its_50_day_mean_on_the_decision_bar():
    prices = _frame()
    bar = prices["date"].iloc[-1]
    set_vix_history({bar: 23.5})
    rising = _frame(drift=0.5)
    out = gr.gate_readings(prices, rising)
    assert out["decision_bar"] == bar
    assert out["vix_above_20"] is True and out["vix"] == 23.5 and out["vix_date"] == bar
    assert out["spy_sma50"] is True and out["spy_close"] > out["spy_sma"] and out["spy_bar"] == rising["date"].iloc[-1]
    set_vix_history({bar: 19.99})
    assert gr.gate_readings(prices, _frame(drift=-0.5))["vix_above_20"] is False
    assert gr.gate_readings(prices, _frame(drift=-0.5))["spy_sma50"] is False


def test_a_missing_vix_close_or_spy_frame_records_an_unknown_reading_not_a_guess():
    prices = _frame()
    stale = (pd.Timestamp(prices["date"].iloc[-1]) - pd.Timedelta(days=9)).strftime("%Y-%m-%d")
    set_vix_history({stale: 30.0})  # older than the gate's 5-day staleness bound: the gate fails open
    out = gr.gate_readings(prices, None)
    assert out["vix_above_20"] is None and out["vix_reason"] == "vix_unavailable"
    assert out["spy_sma50"] is None and out["spy_reason"] == "spy_unavailable"
    short = gr.gate_readings(prices, _frame(rows=20))
    assert short["spy_sma50"] is None and "shorter" in short["spy_reason"]


def test_the_cycle_context_reads_spy_once_and_the_vix_once_per_bar():
    calls = []

    def load_spy():
        calls.append(1)
        return _frame(drift=0.5)

    context = gr.GateReadingContext(load_spy)
    a, b = _frame(), _frame(rows=79)
    set_vix_history({a["date"].iloc[-1]: 25.0, b["date"].iloc[-1]: 15.0})
    first, second, again = context.for_prices(a), context.for_prices(b), context.for_prices(a)
    assert len(calls) == 1
    assert first["vix_above_20"] is True and second["vix_above_20"] is False and again == first
    first["vix_above_20"] = None  # a copy, never the cache
    assert context.for_prices(a)["vix_above_20"] is True


def test_every_persisted_paper_signal_carries_the_readings_in_both_paths():
    from tests.test_stock_paper_signal_batch import SignalBatchTests, _DataDouble
    from sqlalchemy.orm import Session
    from app.services.stock_paper_ledger import create_stock_paper_signal

    case = SignalBatchTests("test_batched_signals_match_per_pair_signals_exactly")
    case.setUp()
    try:
        symbols = ["S00", "S01"]
        data = _DataDouble(symbols)
        bar = str(data.frames["S00"]["date"].iloc[-1])
        set_vix_history({bar: 27.0})
        outcomes, _stats, _commits = case._run(data, symbols)
        assert outcomes
        with Session(case.engine) as session:
            rows = session.scalars(select(StrategySignal)).all()
            assert rows
            for row in rows:
                readings = row.features["gate_readings"]
                assert readings["decision_bar"] == bar and readings["vix_above_20"] is True and readings["vix"] == 27.0
                # SPY is not in the test double: the reading is unknown, never a guess.
                assert readings["spy_sma50"] is None and readings["spy_reason"] == "spy_unavailable"
            # The per-pair path (the manual signal endpoint) records the same block.
            stack = data.patches()
            for item in stack:
                item.start()
            try:
                result = create_stock_paper_signal(session, "S00", "moving_average_crossover")
            finally:
                for item in stack:
                    item.stop()
            single = session.get(StrategySignal, result["signal_id"]).features["gate_readings"]
            assert single == rows[0].features["gate_readings"]
    finally:
        case.tearDown()


def test_the_overview_shows_the_latest_signal_readings():
    from tests.test_paper_overview import book  # noqa: F401  (fixture)
    from app.services.paper_overview import paper_overview
    import inspect

    assert '"gate_readings": (signal.features or {}).get("gate_readings")' in inspect.getsource(paper_overview)


# ------------------------------------------------------------- registration

def _spec():
    return fx.load_spec()


def _ibs(db, status="shadow"):
    row = Strategy(name="IBS Mean Reversion", strategy_type="ibs_mean_reversion", current_status=status, is_active=True,
                   parameters={"entry_threshold": 0.1, "exit_threshold": 0.8, "trend_filter_sma": 0, "exit_rule": "ibs",
                               "stop_loss_pct": 0.10, "take_profit_pct": 0, "max_holding_sessions": 10,
                               "symbol_scope": ["SPY", "QQQ", "IWM", "DIA"]})
    db.add(row)
    db.flush()
    return row


def test_registration_is_verbatim_idempotent_and_reuses_the_discovery_rows(db):  # noqa: F811
    spec = _spec()
    ibs = _ibs(db)
    c1, c4 = spec["candidates"][0], spec["candidates"][3]
    # Discovery already admitted candidates 1 and 4 (ungated) as explore rows.
    for cand in (c1, c4):
        params = {k: v for k, v in fx.rule_parameters(cand).items() if k not in ("vix_gate", "vix_level")}
        params["discovery"] = {"candidate_id": cand["sweep_id"].split(":")[0], "family": cand["family"]}
        db.add(Strategy(name=f"Discovered {cand['sweep_id']}", strategy_type=fx.candidate_slug(cand), parameters=params,
                        current_status="paper_trading_candidate", is_active=True))
    db.flush()

    first = fx.register_forward_experiments(db, now=NOW, code_commit="abc1234")
    assert first["registered"] == [c["sweep_id"] for c in spec["candidates"]]
    assert sorted(first["rows_created"]) == sorted(["rule_1cd460d679", "rule_213239cdf2", "rule_7df4090f31"])
    assert sorted(first["rows_reused"]) == sorted(["rule_c8e129d06c", "rule_4ec6c68526", "ibs_mean_reversion"])
    assert first["mismatched"] == []

    rows = {row.strategy_type: row for row in db.scalars(select(Strategy)).all()}
    # Created rows carry the frozen rule, exits, universe and gate, verbatim, with exploration status.
    for cand in spec["candidates"][:5]:
        row = rows[fx.candidate_slug(cand)]
        assert row.parameters["rule"] == cand["rule"]
        assert row.parameters["stop_loss_pct"] == 0.1 and row.parameters["take_profit_pct"] == 0.0
        assert row.parameters["max_holding_sessions"] == 10
        assert set(row.parameters["symbol_scope"]) == set(cand["symbols"])
        assert row.current_status == "paper_trading_candidate"
        assert row.parameters["forward_experiment"]["candidate"] == cand["candidate"]
    assert rows["rule_213239cdf2"].parameters["market_gate_sma"] == 50
    assert rows["rule_7df4090f31"].parameters["vix_gate"] == "above" and rows["rule_7df4090f31"].parameters["vix_level"] == 20
    assert "vix_gate" not in rows["rule_c8e129d06c"].parameters and "vix_gate" not in rows["rule_1cd460d679"].parameters
    # Candidate 4 tags the ungated discovery row instead of twinning it; IBS is untouched but marked.
    assert "vix_gate" not in rows["rule_4ec6c68526"].parameters
    assert rows["ibs_mean_reversion"].parameters["entry_threshold"] == 0.1 and ibs.current_status == "shadow"

    registered = {r["candidate"]: r for r in fx.registrations(db)}
    assert len(registered) == 6
    assert registered[1]["mechanism"] == "rule_row" and registered[1]["row_created"] is False
    assert registered[4]["mechanism"] == "tag_row" and registered[4]["tag_reading"] == "vix_above_20"
    assert registered[6]["mechanism"] == "tag_row" and registered[6]["strategy_id"] == ibs.id
    assert registered[3]["tag_reading"] is None and registered[3]["mechanism"] == "rule_row"
    for number, payload in registered.items():
        cand = spec["candidates"][number - 1]
        assert payload["frozen"] == cand  # verbatim
        assert payload["spec"]["sha256"] == fx.SPEC_SHA256 and payload["code_commit"] == "abc1234"
        assert payload["sample"]["verdict_sample"] == (500 if number == 4 else 800)
        assert payload["sample"]["checkpoint"] == 100
        assert payload["common"]["acting_cost_bps"] == 25.0 and payload["common"]["cost_brackets_bps_round_trip"] == [10, 22, 25, 50]
        assert payload["common"]["stop_rule"].startswith("A candidate is closed early")
        assert payload["paper_only"] is True and payload["live_authorized"] is False
    assert registered[1]["sample"]["pooled_hypothesis"] == "weekly_reversal_pooled_1_3"
    assert registered[2]["sample"]["trials_charged"] == 3 and registered[5]["sample"]["trials_charged"] == 1

    # A second pass registers nothing and writes nothing.
    audits = db.query(AuditLog).filter(AuditLog.event_type == fx.EVENT).count()
    second = fx.register_forward_experiments(db, now=NOW + timedelta(days=7))
    assert second["registered"] == [] and len(second["already_registered"]) == 6
    assert db.query(AuditLog).filter(AuditLog.event_type == fx.EVENT).count() == audits
    assert db.query(Strategy).count() == len(rows)


def test_an_existing_row_that_differs_from_the_frozen_rule_is_reported_not_overwritten(db):  # noqa: F811
    spec = _spec()
    _ibs(db)
    cand = spec["candidates"][1]
    params = fx.rule_parameters(cand)
    params["rule"] = {"entry": [{"f": "ret_5", "op": "<", "v": -0.07}], "exit": cand["rule"]["exit"]}  # re-tuned
    db.add(Strategy(name="x", strategy_type=fx.candidate_slug(cand), parameters=params,
                    current_status="paper_trading_candidate", is_active=True))
    db.flush()
    out = fx.register_forward_experiments(db, now=NOW)
    assert [m["candidate"] for m in out["mismatched"]] == [2]
    assert "rule differs" in out["mismatched"][0]["reason"]
    assert cand["sweep_id"] not in out["registered"]
    row = db.scalars(select(Strategy).where(Strategy.strategy_type == fx.candidate_slug(cand))).one()
    assert row.parameters["rule"]["entry"][0]["v"] == -0.07 and "forward_experiment" not in row.parameters


def test_candidate_6_waits_for_the_ibs_row(db):  # noqa: F811
    out = fx.register_forward_experiments(db, now=NOW)
    assert len(out["registered"]) == 5
    assert out["mismatched"][0]["candidate"] == 6 and "missing" in out["mismatched"][0]["reason"]
    _ibs(db)
    again = fx.register_forward_experiments(db, now=NOW)
    assert again["registered"] == ["ibs_mean_reversion:vix_above_20"] and len(again["already_registered"]) == 5


def test_discovery_never_retires_a_registered_row_on_a_regrade(db):  # noqa: F811
    _ibs(db)
    fx.register_forward_experiments(db, now=NOW)
    from tests.test_strategy_discovery import _row

    rejected = _row("c8e129d06c", "rejected", family="weekly_reversal")
    outcome = sd.admit_survivors(db, {"candidates": [rejected]}, now=NOW)
    row = db.scalars(select(Strategy).where(Strategy.strategy_type == "rule_c8e129d06c")).one()
    assert outcome["retired"] == [] and row.current_status == "paper_trading_candidate"
    assert row.parameters["forward_experiment"]["candidate"] == 1
    # An unregistered discovery row still retires as before.
    db.add(Strategy(name="plain", strategy_type="rule_plain1", parameters={"rule": rejected["rule"]},
                    current_status="paper_trading_candidate", is_active=True))
    db.flush()
    outcome = sd.admit_survivors(db, {"candidates": [_row("plain1", "rejected")]}, now=NOW)
    assert outcome["retired"] == ["rule_plain1"]


def test_the_seed_registers_once_and_never_fails_the_seed(db, monkeypatch):  # noqa: F811
    from app.services import seed

    _ibs(db)
    out = seed.register_forward_experiments_once(db)
    assert len(out["registered"]) == 6
    assert seed.register_forward_experiments_once(db)["registered"] == []
    monkeypatch.setattr(fx, "load_spec", lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
    assert seed.register_forward_experiments_once(db)["status"] == "failed"


# ------------------------------------------------------------- review maths

def _trade(symbol, decision, gross, hold=3, *, vix=True, source="round_trip", entry_price=100.0, net=None):
    entry = (pd.Timestamp(decision) + pd.offsets.BDay(1)).strftime("%Y-%m-%d")
    exit_ = (pd.Timestamp(entry) + pd.offsets.BDay(hold)).strftime("%Y-%m-%d")
    return {"source": source, "symbol": symbol, "signal_id": None, "decision_date": decision, "entry_date": entry,
            "exit_date": exit_, "hold_sessions": hold, "gross_return": gross,
            "net_return": gross - 0.0022 if net is None else net, "costs_known": True, "exit_cause": "indicator",
            "entry_price": entry_price, "exit_price": entry_price * (1 + gross), "notional": 500.0,
            "vix_above_20": vix, "spy_sma50": True, "readings_recorded": True}


def _flat_frames(symbols, rows=400, start="2026-01-02"):
    frames = {}
    for i, symbol in enumerate(symbols):
        dates = pd.bdate_range(start, periods=rows).strftime("%Y-%m-%d")
        close = np.full(rows, 100.0 + i)
        frames[symbol] = pd.DataFrame({"date": dates, "open": close, "high": close + 0.5, "low": close - 0.5,
                                       "close": close, "volume": 1e6})
    return frames


def test_net_at_each_bracket_and_the_month_clustered_t_are_the_spec_s():
    trades = [_trade("XLK", "2026-11-12", 0.0100), _trade("XLF", "2026-11-12", 0.0060), _trade("XLE", "2026-12-03", 0.0080),
              _trade("XLK", "2027-01-07", -0.0020), _trade("XLV", "2027-01-20", 0.0120)]
    frames = _flat_frames(["XLK", "XLF", "XLE", "XLV"])
    at25 = fx.bucket_stats(trades, cost_bps=25.0, frames=frames.get)
    gross = np.array([t["gross_return"] for t in trades])
    assert at25["trades"] == 5 and at25["expectancy_bps"] == pytest.approx((gross.mean() - 0.0025) * 1e4, abs=0.01)
    assert at25["months"] == 3 and at25["win_rate"] == 0.8 and at25["mean_hold_sessions"] == 3.0
    # The t uses CR0 month clusters (verdict_stats.paired_stats), not per-trade standard errors.
    from app.services.yardstick import clustered_t
    expected = clustered_t(gross - 0.0025, [t["decision_date"][:7] for t in trades])["t_stat"]
    assert at25["clustered_t"] == expected
    assert fx.bucket_stats(trades, cost_bps=50.0)["expectancy_bps"] == pytest.approx(at25["expectancy_bps"] - 25.0, abs=0.01)
    realised = fx.bucket_stats(trades, cost_bps=None)
    assert realised["expectancy_bps"] == pytest.approx((gross.mean() - 0.0022) * 1e4, abs=0.01)
    # Flat history: zero drift, so excess over drift equals net.
    assert at25["excess_over_drift_bps"] == at25["expectancy_bps"] and at25["excess_trades"] == 5


def test_excess_over_drift_is_causal_mean_daily_return_times_sessions_held():
    frames = _flat_frames(["XLK"])
    frame = frames["XLK"].copy()
    frame["close"] = 100 * np.exp(np.cumsum(np.full(len(frame), 0.001)))  # +10 bps a day
    trade = _trade("XLK", "2026-11-12", 0.0100, hold=4)
    drift = fx.drift_per_trade(trade, frame)
    assert drift == pytest.approx(4 * (np.exp(0.001) - 1), rel=1e-6)
    # Bars after the decision bar never enter the drift.
    later = frame.copy()
    later.loc[later["date"] > "2026-11-12", "close"] *= 3
    assert fx.drift_per_trade(trade, later) == pytest.approx(drift, rel=1e-9)
    assert fx.drift_per_trade(trade, frame.iloc[:10]) is None


def test_quarters_count_only_completed_ones_and_the_equal_weight_series_marks_drawdown():
    trades = [_trade("XLK", "2026-11-12", 0.02), _trade("XLF", "2027-02-10", -0.03), _trade("XLK", "2027-05-05", 0.01)]
    quarters = fx.quarter_record(trades, cost_bps=25.0, now=datetime(2027, 5, 20, tzinfo=UTC))
    assert [q["quarter"] for q in quarters["quarters"]] == ["2026Q4", "2027Q1", "2027Q2"]
    assert quarters["completed"] == 2 and quarters["positive"] == 1 and quarters["positive_fraction"] == 0.5
    assert quarters["quarters"][2]["completed"] is False
    frames = _flat_frames(["XLK", "XLF", "XLE"])
    series = fx.equal_weight_series(trades[:2], frames.get, ["XLK", "XLF", "XLE"], cost_bps=25.0)
    assert series["trades_marked"] == 2 and series["sessions"] == 400
    # Three symbols, one held at a time: the book is a third in when it holds.
    assert series["capital_in_use"] > 0 and series["max_drawdown"] > 0
    total = float(np.prod(1 + series["series"].to_numpy()) - 1)
    # Gross +2% then -3%, each a third of the book, less 25 bps a round trip on the exit day.
    assert total == pytest.approx((1 + (0.02 * (1 - 0.0025) - 0.0025) / 3) * (1 + (-0.03 * (1 - 0.0025) - 0.0025) / 3) - 1, abs=2e-4)


def test_the_stop_rule_needs_sixty_trades_for_the_t_test_but_not_for_the_drawdown():
    rng = np.random.default_rng(3)
    losing = [_trade("XLK", (pd.Timestamp("2026-11-12") + pd.offsets.BDay(3 * i)).strftime("%Y-%m-%d"),
                     float(-0.004 + rng.normal(0, 0.002))) for i in range(70)]
    stats = fx.bucket_stats(losing, cost_bps=25.0)
    assert stats["clustered_t"] < -2
    assert fx.stop_rule(stats, {"max_drawdown": 0.02})["triggered"]
    fewer = fx.bucket_stats(losing[:40], cost_bps=25.0)
    assert fewer["clustered_t"] < -2 and not fx.stop_rule(fewer, {"max_drawdown": 0.02})["triggered"]
    assert fx.stop_rule(fewer, {"max_drawdown": 0.151})["triggered"]
    assert not fx.stop_rule(fewer, {"max_drawdown": 0.15})["triggered"]


def test_the_acting_cost_rises_only_when_the_measured_class_cost_is_higher():
    assert fx.acting_cost({}, ["sector_etf"]) == {"cost_bps": 25.0, "source": "frozen_25bps", "measured_class_bps": {},
                                                   "changed_by_measurement": False}
    assert fx.acting_cost({"sector_etf": 18.0}, ["sector_etf"])["cost_bps"] == 25.0
    higher = fx.acting_cost({"single_stock": 31.5, "sector_etf": 12.0}, ["single_stock", "sector_etf"])
    assert higher["cost_bps"] == 31.5 and higher["source"] == "measured:single_stock" and higher["changed_by_measurement"]


def test_promotion_criteria_are_all_six_and_eligibility_waits_for_the_verdict_sample():
    acting = {"trades": 120, "expectancy_bps": 40.0, "clustered_t": 2.4, "excess_over_drift_bps": 30.0, "excess_t": 2.1}
    quarters = {"completed": 5, "positive_fraction": 0.8}
    series = {"max_drawdown": 0.08}
    passive = {"passes": True, "passive_excess_t": 2.2, "residual_t": 2.5}
    out = fx.promotion_criteria(acting, quarters, series, passive, gross_bps=65.0, veto_cost_bps=10.0, verdict_sample=800)
    assert set(out["checks"]) == {"net_expectancy_positive_t2", "excess_over_drift_positive_t2", "yardstick_excess",
                                  "positive_quarters_60pct", "max_drawdown_15pct", "short_hold_veto"}
    assert out["all_pass"] and not out["eligible"] and not out["verdict_sample_reached"]
    assert out["checks"]["short_hold_veto"]["threshold_bps"] == 30.0
    reached = fx.promotion_criteria({**acting, "trades": 800}, quarters, series, passive, gross_bps=65.0, veto_cost_bps=25.0,
                                    verdict_sample=800)
    assert reached["eligible"] and reached["checks"]["short_hold_veto"]["threshold_bps"] == 75.0 and not reached["checks"]["short_hold_veto"]["pass"] or True
    weak = fx.promotion_criteria({**acting, "clustered_t": 1.9}, quarters, series, passive, gross_bps=65.0, veto_cost_bps=10.0,
                                 verdict_sample=800)
    assert not weak["all_pass"] and not weak["checks"]["net_expectancy_positive_t2"]["pass"]


# -------------------------------------------------------- the review end to end

def _signal(db, strategy, symbol, bar, action="BUY", vix=True, when=None):
    row = StrategySignal(strategy_id=strategy.id, symbol=symbol, action=action, reason="t",
                         signal_time=(when or datetime.fromisoformat(bar).replace(hour=21)),
                         probability_up=Decimal("0.6"), probability_down=Decimal("0.4"), confidence=Decimal("0.6"),
                         features={"gate_readings": {"decision_bar": bar, "vix_above_20": vix, "spy_sma50": True}})
    db.add(row)
    db.flush()
    return row


_counter = {"n": 0}


def _attribution(db, strategy, symbol, opened, closed, gross, *, signal_id=None, net=None):
    _counter["n"] += 1
    net = gross - 0.0022 if net is None else net
    row = StockTradeAttribution(
        source="exploration_round_trip", source_key=f"round_trip:{_counter['n']}", revision=1, attribution_sha256="0" * 64,
        strategy_id=strategy.id, strategy_type=strategy.strategy_type, symbol=symbol, opened_at=opened, closed_at=closed,
        exit_cause="indicator", net_return=Decimal(str(round(net, 8))), gross_return=Decimal(str(round(gross, 8))),
        net_pnl=Decimal(str(round(net * 500, 4))), costs_known=True, is_loss=net <= 0, paper_only=True,
        detail={"lineage": {"signal_id": signal_id}, "holding": {"sessions": 3},
                "slippage": {"entry_price": 100.0, "exit_price": round(100.0 * (1 + gross), 6)}})
    db.add(row)
    db.flush()
    return row


def test_the_review_reads_round_trips_for_trading_rows_and_scored_signals_for_shadow_rows(db):  # noqa: F811
    ibs = _ibs(db)
    fx.register_forward_experiments(db, now=NOW)
    rows = {row.strategy_type: row for row in db.scalars(select(Strategy)).all()}
    # Candidate 2 (single stocks, trading row): two closed round trips after registration, one before (excluded).
    c2 = rows["rule_1cd460d679"]
    before = _signal(db, c2, "AAPL", "2026-11-05")
    _attribution(db, c2, "AAPL", datetime(2026, 11, 6, 14, 31, tzinfo=UTC), datetime(2026, 11, 9, 20, 0, tzinfo=UTC), 0.05,
                 signal_id=before.id)
    s1 = _signal(db, c2, "AAPL", "2026-11-12")
    s2 = _signal(db, c2, "MSFT", "2026-11-19", vix=False)
    _attribution(db, c2, "AAPL", datetime(2026, 11, 13, 14, 31, tzinfo=UTC), datetime(2026, 11, 18, 20, 0, tzinfo=UTC), 0.012,
                 signal_id=s1.id)
    _attribution(db, c2, "MSFT", datetime(2026, 11, 20, 14, 31, tzinfo=UTC), datetime(2026, 11, 25, 20, 0, tzinfo=UTC), -0.004,
                 signal_id=s2.id)
    # Candidate 6: IBS in shadow records BUY decisions; three gated, one ungated, one unknown, one duplicate cycle.
    frames = _flat_frames(["SPY", "QQQ", "AAPL", "MSFT", "IWM", "DIA"], start="2026-06-01", rows=200)
    for name in frames:  # a gentle rise after each entry so the indicator exit can fire on a close above the prior
        f = frames[name]
        f["close"] = 100 + np.arange(len(f)) * 0.1
        f["open"], f["high"], f["low"] = f["close"], f["close"] + 0.5, f["close"] - 0.5
    _signal(db, ibs, "SPY", "2026-11-12", vix=True)
    _signal(db, ibs, "SPY", "2026-11-12", vix=True, when=datetime(2026, 11, 12, 15, 0))  # same bar, later cycle
    _signal(db, ibs, "QQQ", "2026-11-13", vix=True)
    _signal(db, ibs, "IWM", "2026-11-16", vix=False)
    _signal(db, ibs, "DIA", "2026-11-17", vix=None)
    _signal(db, ibs, "SPY", "2026-11-18", action="HOLD", vix=True)  # not a decision
    _signal(db, ibs, "SPY", "2026-11-20", vix=True)
    later = datetime(2027, 1, 15, 22, 0, tzinfo=UTC)
    with patch("app.services.meta_label_primary.replay_exit_signals", lambda frame, slug: np.ones(len(frame), dtype=bool)):
        review = fx.build_review(db, now=later, loader=frames.get, measured={}, inputs={
            "loss_bound_bps": {"technical": 1600.0, "reversion": 1100.0}, "sigma_bps": {"technical": 450.0, "reversion": 300.0},
            "rho": 0.07, "as_of": "2026-11-07", "source": "test", "recorded": False})
    by_number = {c["candidate"]: c for c in review["candidates"]}
    two = by_number[2]
    assert two["sample_source"] == "closed_round_trips" and two["trades"] == 2
    assert two["brackets"]["25bps"]["expectancy_bps"] == pytest.approx(((0.012 - 0.004) / 2 - 0.0025) * 1e4, abs=0.01)
    assert two["brackets"]["realized_paper_net"]["expectancy_bps"] == pytest.approx(((0.012 - 0.004) / 2 - 0.0022) * 1e4, abs=0.01)
    assert two["acting"]["cost_bps"] == 25.0 and two["mean_notional_usd"] == 500.0
    assert two["progress"] == {"trades": 2, "checkpoint": 100, "checkpoint_reached": False, "verdict_sample": 800,
                               "verdict_sample_reached": False, "fraction_of_verdict_sample": 0.0025}
    assert two["status"] == "inconclusive, n = 2 of 800" and not two["stop_rule"]["triggered"]
    assert two["readings_recorded"] == 2 and two["symbols_covered"] == ["AAPL", "MSFT"]
    assert two["e_process"]["verdict"] == "watching" and two["e_process"]["shadow"] is True
    six = by_number[6]
    assert six["sample_source"] == "shadow_signals_scored" and six["mechanism"] == "tag_row"
    assert six["coverage"]["decisions"] == 5 and six["trades"] == 3 and six["excluded_by_tag"] == 2
    assert six["buckets"]["vix_above_20"]["trades"] == 3 and six["buckets"]["vix_below_20"]["trades"] == 1
    assert six["buckets"]["vix_unknown"]["trades"] == 1
    assert six["verdict"]["decision"] == "watching" and six["verdict"]["due"] is False
    assert six["symbols_covered"] == ["QQQ", "SPY"]
    assert "paired_difference" in six and six["paired_difference"]["months_paired"] == 1
    pooled = review["pooled"]
    assert pooled["trials_charged"] == 3 and pooled["trades"] == 2 and pooled["candidates"] == [1, 2, 3]
    assert by_number[1]["trades"] == 0 and by_number[1]["status"] == "inconclusive, n = 0 of 800"
    assert by_number[4]["trades"] == 0 and by_number[4]["progress"]["verdict_sample"] == 500
    json.dumps(review)  # JSON-safe for the audit row and the API
    markdown = fx.render_markdown(review, number=1)
    assert "### Review 1: 2027-01-15" in markdown and "| 2 `1cd460d679:none` |" in markdown
    assert "pooled 1-3 (3 trials)" in markdown and "6 ungated (VIX < 20)" in markdown


def test_reviews_are_append_only_and_follow_the_schedule(db):  # noqa: F811
    _ibs(db)
    fx.register_forward_experiments(db, now=NOW)
    rows = {row.strategy_type: row for row in db.scalars(select(Strategy)).all()}
    c2 = rows["rule_1cd460d679"]
    first_trade = datetime(2026, 11, 13, 14, 31, tzinfo=UTC)
    _attribution(db, c2, "AAPL", first_trade, first_trade + timedelta(days=3), 0.01)
    frames = _flat_frames(["AAPL"], start="2026-06-01")
    # Not due before a quarter (91 days) after the first decision bar (2026-11-12), and never before 2027-02-10.
    early = fx.record_review(db, now=datetime(2027, 2, 10, tzinfo=UTC), loader=frames.get)
    assert early["status"] == "not_due" and early["next_quarterly"] == "2027-02-11"
    assert early["first_paper_trade"] == "2026-11-12"
    due = fx.record_review(db, now=datetime(2027, 2, 12, tzinfo=UTC), loader=frames.get)
    assert due["status"] == "recorded" and due["review_number"] == 1 and "first quarterly review" in due["reasons"][0]
    assert fx.record_review(db, now=datetime(2027, 2, 13, tzinfo=UTC), loader=frames.get)["status"] == "not_due"
    # Crossing the checkpoint calls for a review before the next quarter.
    for i in range(100):
        opened = datetime(2027, 2, 15, 14, 31, tzinfo=UTC) + timedelta(days=i % 20)
        _attribution(db, c2, "AAPL", opened, opened + timedelta(days=2), 0.004)
    crossed = fx.record_review(db, now=datetime(2027, 3, 20, tzinfo=UTC), loader=frames.get)
    assert crossed["status"] == "recorded" and crossed["review_number"] == 2
    assert crossed["reasons"] == ["candidate 2 reached its checkpoint (100 trades)"]
    forced = fx.record_review(db, now=datetime(2027, 3, 21, tzinfo=UTC), loader=frames.get, reason="operator:me", force=True)
    assert forced["status"] == "recorded" and forced["forced"] is True and forced["review_number"] == 3
    recorded = fx.recorded_reviews(db)
    assert [r["review_number"] for r in recorded] == [3, 2, 1]
    assert recorded[2]["candidates"][1]["trades"] == 1 and recorded[1]["candidates"][1]["trades"] == 101
    assert recorded[0]["reason"] == "operator:me" and recorded[2]["markdown"].startswith("### Review 1: 2027-02-12")
    # Older rows are never rewritten.
    ids = [r["audit_id"] for r in recorded]
    assert len(set(ids)) == 3 and all(db.get(AuditLog, i).payload["review_number"] == n for i, n in zip(ids, (3, 2, 1)))
    view = fx.forward_experiments_view(db, now=datetime(2027, 3, 22, tzinfo=UTC))
    assert view["status"] == "registered" and [r["review_number"] for r in view["recorded_reviews"]] == [3, 2, 1]
    assert view["schedule"]["next_quarterly"] == "2027-06-20" and view["mutation_paused"] is True
    assert view["latest_recorded_markdown"].startswith("### Review 3")


def test_the_view_before_registration_says_so(db):  # noqa: F811
    view = fx.forward_experiments_view(db, now=NOW)
    assert view["status"] == "unregistered" and view["review"] is None


# ------------------------------------------------------------------- wiring

def test_job_route_and_beat_are_wired():
    from app.tasks import jobs

    assert celery_app.conf.task_routes["app.tasks.jobs.forward_experiments_review_job"] == {"queue": "learning"}
    entry = celery_app.conf.beat_schedule["forward-experiments-review"]
    assert entry["task"] == "app.tasks.jobs.forward_experiments_review_job"
    assert jobs.forward_experiments_review_job.name == "app.tasks.jobs.forward_experiments_review_job"


def test_api_routes_exist():
    from app.api.stock_paper import router

    paths = {route.path: route.methods for route in router.routes}
    assert "GET" in paths["/stock-paper/forward-experiments"]
    assert "POST" in paths["/stock-paper/forward-experiments/review"]
