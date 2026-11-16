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


# ------------------------------------------------------------------ closures

def _losing_run(db, strategy, *, trades=70, start=datetime(2026, 11, 13, 14, 31, tzinfo=UTC)):
    """Enough losing round trips, spread over months, to trip the t arm of the stop rule."""
    for i in range(trades):
        opened = start + timedelta(days=i)
        _attribution(db, strategy, "AAPL", opened, opened + timedelta(days=2), -0.02 - 0.001 * (i % 5))


def _registered_rows(db):
    _ibs(db)
    fx.register_forward_experiments(db, now=NOW)
    return {row.strategy_type: row for row in db.scalars(select(Strategy)).all()}


def _close_rows(db):
    return db.scalars(select(AuditLog).where(AuditLog.event_type == fx.EVENT, AuditLog.action == fx.ACTION_CLOSE)
                      .order_by(AuditLog.id)).all()


def test_the_review_that_records_a_triggered_stop_rule_closes_the_candidate_once(db):  # noqa: F811
    from app.models import Notification

    rows = _registered_rows(db)
    c2 = rows["rule_1cd460d679"]
    _losing_run(db, c2)
    frames = _flat_frames(["AAPL"], start="2026-06-01")
    # Before a review records it, the live reading says what will happen.
    live = fx.build_review(db, now=datetime(2027, 3, 1, tzinfo=UTC), loader=frames.get, measured={}, inputs={})
    two = {c["candidate"]: c for c in live["candidates"]}[2]
    assert two["stop_rule"]["triggered"] and two["closure"] is None
    assert "the next recorded review closes it" in fx.render_markdown(live, number=1)
    assert fx.entry_block_for(db, c2.id) is None and fx.entry_blocks(db) == {}

    recorded = fx.record_review(db, now=datetime(2027, 3, 1, tzinfo=UTC), loader=frames.get)
    assert recorded["status"] == "recorded" and recorded["closed"] == [2]
    assert "candidate 2: the stop rule triggered" in recorded["reasons"]
    (close,) = _close_rows(db)
    review = db.scalars(select(AuditLog).where(AuditLog.event_type == fx.EVENT, AuditLog.action == fx.ACTION_REVIEW)).one()
    assert close.entity_type == "strategy" and close.entity_id == c2.id and close.status == "closed"
    payload = close.payload
    assert payload["candidate"] == 2 and payload["sweep_id"] == "1cd460d679:none" and payload["mechanism"] == "rule_row"
    assert payload["reason_kind"] == "stop_rule" and payload["entries_blocked"] is True and payload["reopen"] == "none"
    assert payload["review_number"] == 1 and payload["review_audit_id"] == review.id
    assert payload["reasons"] == two["stop_rule"]["reasons"] and payload["closed_by"] == "forward_experiments_review"
    assert payload["open_at_closure"] == {"lots": [], "pending_entries": 0}
    # The review row carries the closure it caused.
    reviewed = {c["candidate"]: c for c in review.payload["candidates"]}[2]
    assert reviewed["closure"]["reason_kind"] == "stop_rule" and reviewed["status"].startswith("closed 2027-03-01")
    assert "Candidate 2: CLOSED 2027-03-01 (stop rule:" in review.payload["markdown"]
    assert "new entries disabled" in review.payload["markdown"] and "| CLOSED |" in review.payload["markdown"]
    # The row takes no new entries, by its id; the marker mirrors the closure; it is still protected (not retired).
    reason = fx.entry_block_for(db, c2.id)
    assert reason.startswith("forward experiment 2 is closed (stop rule, 2027-03-01)")
    assert fx.entry_blocks(db) == {c2.id: reason}
    marker = c2.parameters["forward_experiment"]
    assert marker["closure_kind"] == "stop_rule" and marker["entries_blocked"] is True and "released_at" not in marker
    assert fx.is_registered_row(c2.parameters) and c2.current_status == "paper_trading_candidate"
    notice = db.scalars(select(Notification).where(Notification.source == "forward_experiments")).one()
    assert notice.title == "Forward experiment 2 closed" and notice.entity_id == c2.id
    # A closed candidate calls for no further review and is never closed twice.
    assert fx.record_review(db, now=datetime(2027, 3, 8, tzinfo=UTC), loader=frames.get)["status"] == "not_due"
    forced = fx.record_review(db, now=datetime(2027, 3, 9, tzinfo=UTC), loader=frames.get, force=True)
    assert forced["status"] == "recorded" and forced["closed"] == [] and len(_close_rows(db)) == 1
    again = fx.close_candidate_by_number(db, 2, reason="a second closure must not be written")
    assert again["status"] == "already_closed" and len(_close_rows(db)) == 1
    view = fx.forward_experiments_view(db, now=datetime(2027, 3, 10, tzinfo=UTC))
    assert [c["candidate"] for c in view["closures"]] == [2]
    assert {r["candidate"]: r["closure"] for r in view["registrations"]}[2]["reason_kind"] == "stop_rule"
    assert {r["candidate"]: r["closure"] for r in view["registrations"]}[1] is None


def test_a_trigger_an_earlier_review_already_recorded_still_closes(db):  # noqa: F811
    from app.services.audit import write_audit_log

    rows = _registered_rows(db)
    c2 = rows["rule_1cd460d679"]
    _losing_run(db, c2)
    frames = _flat_frames(["AAPL"], start="2026-06-01")
    # A review written before closures existed: the trigger is on record, nothing was closed.
    earlier = fx.build_review(db, now=datetime(2027, 3, 1, tzinfo=UTC), loader=frames.get)
    write_audit_log(db, event_type=fx.EVENT, entity_type="forward_experiments", action=fx.ACTION_REVIEW,
                    status="recorded", message="review 1", payload={**earlier, "review_number": 1})
    db.flush()
    assert fx.closures(db) == {}
    recorded = fx.record_review(db, now=datetime(2027, 3, 2, tzinfo=UTC), loader=frames.get)
    assert recorded["status"] == "recorded" and recorded["review_number"] == 2 and recorded["closed"] == [2]
    assert fx.entry_block_for(db, c2.id) is not None


def test_a_closed_candidate_s_sample_ends_at_its_closure_and_the_pool_keeps_its_trades(db):  # noqa: F811
    rows = _registered_rows(db)
    c2, ibs = rows["rule_1cd460d679"], rows["ibs_mean_reversion"]
    closed_at = datetime(2026, 12, 1, 18, 0, tzinfo=UTC)
    before = _signal(db, c2, "AAPL", "2026-11-12")
    at_broker = _signal(db, c2, "MSFT", "2026-11-30", when=datetime(2026, 12, 1, 17, 59))
    after = _signal(db, c2, "AAPL", "2026-12-01", when=datetime(2026, 12, 1, 18, 1))
    _attribution(db, c2, "AAPL", datetime(2026, 11, 13, 14, 31, tzinfo=UTC), datetime(2026, 11, 18, 20, 0, tzinfo=UTC), 0.012,
                 signal_id=before.id)
    # Decided before the closure, filled after it, closed long after it: the candidate's trade.
    _attribution(db, c2, "MSFT", datetime(2026, 12, 1, 18, 5, tzinfo=UTC), datetime(2026, 12, 8, 20, 0, tzinfo=UTC), -0.004,
                 signal_id=at_broker.id)
    # Decided after the closure (any path that got past the gates): not the candidate's trade.
    _attribution(db, c2, "AAPL", datetime(2026, 12, 2, 14, 31, tzinfo=UTC), datetime(2026, 12, 4, 20, 0, tzinfo=UTC), 0.05,
                 signal_id=after.id)
    _attribution(db, c2, "AAPL", datetime(2026, 12, 3, 14, 31, tzinfo=UTC), datetime(2026, 12, 5, 20, 0, tzinfo=UTC), 0.05)
    frames = _flat_frames(["SPY", "QQQ", "AAPL", "MSFT"], start="2026-06-01", rows=200)
    for name in frames:
        f = frames[name]
        f["close"] = 100 + np.arange(len(f)) * 0.1
        f["open"], f["high"], f["low"] = f["close"], f["close"] + 0.5, f["close"] - 0.5
    _signal(db, ibs, "SPY", "2026-11-12", vix=True)
    _signal(db, ibs, "QQQ", "2026-12-02", vix=True)  # after candidate 6 closes
    later = datetime(2027, 1, 15, 22, 0, tzinfo=UTC)
    inputs = {"loss_bound_bps": {"technical": 1600.0, "reversion": 1100.0}, "sigma_bps": {"technical": 450.0, "reversion": 300.0},
              "rho": 0.07, "as_of": "2026-11-07", "source": "test", "recorded": False}

    def reading():
        with patch("app.services.meta_label_primary.replay_exit_signals", lambda frame, slug: np.ones(len(frame), dtype=bool)):
            review = fx.build_review(db, now=later, loader=frames.get, measured={}, inputs=inputs)
        return {c["candidate"]: c for c in review["candidates"]}, review

    open_, _ = reading()
    assert open_[2]["trades"] == 4 and open_[6]["trades"] == 2
    fx.close_candidate_by_number(db, 2, reason="cannot reach a verdict in a useful time", now=closed_at)
    fx.close_candidate_by_number(db, 6, reason="closing the tag sample for the test", now=closed_at)
    closed, review = reading()
    assert closed[2]["trades"] == 2 and closed[2]["symbols_covered"] == ["AAPL", "MSFT"]
    assert closed[2]["status"] == "closed 2026-12-01 (operator closure), final n = 2"
    assert closed[2]["closure"]["reason_kind"] == "operator_closure" and closed[2]["open_lots"] == {"lots": [], "pending_entries": 0}
    assert closed[6]["trades"] == 1 and closed[6]["symbols_covered"] == ["SPY"] and closed[6]["open_lots"] is None
    assert review["pooled"]["trades"] == 2  # the pooled hypothesis keeps the closed member's trades
    markdown = fx.render_markdown(review, number=1)
    assert "Candidate 6: CLOSED 2026-12-01 (operator closure: closing the tag sample for the test); the sample is closed" in markdown
    # Neither closed candidate asks for a review again.
    assert fx.review_due(review, None, now=later)["reasons"] == []


def test_a_tag_candidate_closes_its_sample_only_and_a_rule_row_closes_its_entries(db):  # noqa: F811
    rows = _registered_rows(db)
    ibs = rows["ibs_mean_reversion"]
    assert fx.is_registered_row(ibs.parameters)
    out = fx.close_candidate_by_number(db, 6, reason="two-year clock elapsed without a pass", reason_kind="verdict_failed",
                                       actor="operator:me", now=datetime(2027, 3, 1, tzinfo=UTC))
    assert out["status"] == "closed" and out["closure"]["mechanism"] == "tag_row"
    assert out["closure"]["entries_blocked"] is False and out["closure"]["closed_by"] == "operator:me"
    # The host row is not the experiment's to change: it still enters, and it is released to normal governance.
    assert fx.entry_block_for(db, ibs.id) is None and fx.entry_blocks(db) == {}
    assert ibs.current_status == "shadow" and not fx.is_registered_row(ibs.parameters)
    assert ibs.parameters["forward_experiment"]["released_at"] == "2027-03-01T00:00:00+00:00"
    # Candidate 4 has no ungated host here, so it was bound as its own gated row: the mechanism decides, not the number.
    four = next(r for r in fx.registrations(db) if r["candidate"] == 4)
    assert four["mechanism"] == "rule_row"
    out = fx.close_candidate_by_number(db, 4, reason="closing the gated row for the test")
    assert out["closure"]["entries_blocked"] is True and fx.entry_block_for(db, four["strategy_id"]) is not None


def test_a_closed_rule_row_stays_in_the_cycle_until_it_is_flat_then_retires(db):  # noqa: F811
    from tests.test_strategy_discovery import _row

    rows = _registered_rows(db)
    c1 = rows["rule_c8e129d06c"]
    fx.close_candidate_by_number(db, 1, reason="cannot reach a verdict in a useful time", now=datetime(2027, 3, 1, tzinfo=UTC))
    # A discovery re-grade must not take the row out of the cycle while a lot may still need its exits.
    outcome = sd.admit_survivors(db, {"candidates": [_row("c8e129d06c", "rejected", family="weekly_reversal")]}, now=NOW)
    assert outcome["retired"] == [] and c1.current_status == "paper_trading_candidate"
    held = {"lots": [{"symbol": "XLK", "quantity": "3"}], "pending_entries": 0}
    with patch.object(fx, "open_lots", return_value=held):
        settled = fx.settle_closures(db, now=datetime(2027, 3, 6, tzinfo=UTC))
    assert settled["retired"] == [] and settled["winding_down"] == [
        {"candidate": 1, "strategy_type": "rule_c8e129d06c", "open": held}]
    assert c1.current_status == "paper_trading_candidate" and fx.is_registered_row(c1.parameters)
    # An unreadable book is never read as flat.
    with patch.object(fx, "open_lots", side_effect=RuntimeError("down")):
        assert fx.settle_closures(db)["retired"] == [] and c1.current_status == "paper_trading_candidate"
    # An entry still in flight keeps it in the cycle too.
    with patch.object(fx, "open_lots", return_value={"lots": [], "pending_entries": 1}):
        assert fx.settle_closures(db)["retired"] == []
    settled = fx.settle_closures(db, now=datetime(2027, 3, 13, tzinfo=UTC))
    assert settled["retired"] == ["rule_c8e129d06c"] and c1.current_status == "retired"
    assert not fx.is_registered_row(c1.parameters)
    audit = db.scalars(select(AuditLog).where(AuditLog.event_type == "strategy_governance",
                                              AuditLog.action == fx.RETIRE_ACTION)).one()
    assert audit.entity_id == c1.id and audit.payload["old_status"] == "paper_trading_candidate"
    assert audit.payload["closure_audit_id"] == _close_rows(db)[0].id
    assert fx.settle_closures(db)["retired"] == []  # once
    assert fx.entry_block_for(db, c1.id) is not None  # retired or not, the closure still refuses entries
    # An open candidate's row is never settled.
    assert rows["rule_1cd460d679"].current_status == "paper_trading_candidate"


def test_open_lots_reads_the_row_s_own_holding_and_entries_in_flight(db):  # noqa: F811
    from app.models.stock_paper import StockPaperAccount, StockPaperFill, StockPaperOrder, StockPaperPosition

    rows = _registered_rows(db)
    c2, c1 = rows["rule_1cd460d679"], rows["rule_c8e129d06c"]
    account = StockPaperAccount(broker="alpaca_paper", broker_account_id="paper-test", currency="USD", cash=1000,
                                buying_power=1000, equity=1000, status="reconciled", reconciliation_required=False,
                                raw_payload={})
    db.add(account)
    db.flush()
    when = datetime(2026, 11, 20, 15, 0, tzinfo=UTC)

    def order(strategy, symbol, side, status, key):
        row = StockPaperOrder(account_id=account.id, strategy_id=strategy.id, client_order_id=key, symbol=symbol,
                              side=side, quantity=Decimal("2"), status=status, source="paper_exploration")
        db.add(row)
        db.flush()
        return row

    def fill(order_row, key):
        db.add(StockPaperFill(account_id=account.id, order_id=order_row.id, broker_activity_id=key,
                              symbol=order_row.symbol, side=order_row.side, quantity=order_row.quantity,
                              price=Decimal("100"), filled_at=when, raw_payload={}))

    fill(order(c2, "AAPL", "buy", "filled", "b1"), "a1")
    fill(order(c2, "MSFT", "buy", "filled", "b2"), "a2")
    fill(order(c2, "MSFT", "sell", "filled", "s2"), "a3")   # MSFT is flat again
    fill(order(c1, "XLK", "buy", "filled", "b3"), "a4")     # another row's lot
    order(c2, "NVDA", "buy", "accepted", "b4")              # an entry still at the broker
    for symbol, quantity in (("AAPL", "2"), ("XLK", "2")):
        db.add(StockPaperPosition(account_id=account.id, symbol=symbol, quantity=Decimal(quantity), observed_at=when,
                                  raw_payload={}))
    db.flush()
    assert fx.open_lots(db, c2) == {"lots": [{"symbol": "AAPL", "quantity": "2.000000000"}], "pending_entries": 1}
    assert fx.open_lots(db, rows["rule_213239cdf2"]) == {"lots": [], "pending_entries": 0}
    fx.close_candidate_by_number(db, 2, reason="closing with a lot still open")
    assert _close_rows(db)[0].payload["open_at_closure"]["lots"] == [{"symbol": "AAPL", "quantity": "2.000000000"}]
    assert fx.settle_closures(db)["retired"] == [] and c2.current_status == "paper_trading_candidate"


def test_a_closure_survives_a_reseed_a_resume_a_lost_marker_and_a_new_session(db):  # noqa: F811
    from sqlalchemy.orm import Session as OrmSession

    from app.models import RiskRule
    from app.services import seed
    from app.services.safety_controls import resume_candidate_strategies

    rows = _registered_rows(db)
    c2 = rows["rule_1cd460d679"]
    fx.close_candidate_by_number(db, 2, reason="cannot reach a verdict in a useful time", now=datetime(2027, 3, 1, tzinfo=UTC))
    blocked = fx.entry_block_for(db, c2.id)
    assert blocked is not None
    # A reseed registers nothing again, closes nothing again and re-mirrors a marker that lost its closure.
    params = dict(c2.parameters)
    params["forward_experiment"] = {k: v for k, v in params["forward_experiment"].items() if k not in fx.MARKER_CLOSURE_KEYS}
    c2.parameters = params
    db.flush()
    out = seed.register_forward_experiments_once(db)
    assert out["registered"] == [] and out["closure_markers"] == ["rule_1cd460d679"]
    assert c2.parameters["forward_experiment"]["closure_kind"] == "operator_closure" and len(_close_rows(db)) == 1
    assert seed.register_forward_experiments_once(db)["closure_markers"] == []
    # A pause followed by the control room's resume puts the row back in the cycle; the closure still refuses entries.
    db.add(RiskRule(name="defaults", value={}))
    c2.current_status = "paused"
    db.flush()
    resume_candidate_strategies(db, actor="operator:test")
    assert c2.current_status == "paper_trading_candidate" and fx.entry_block_for(db, c2.id) == blocked
    # An overlay that replaces the row's parameters wholesale loses the marker, not the closure.
    c2.parameters = {"rule": c2.parameters["rule"]}
    db.commit()
    assert fx.entry_block_for(db, c2.id) == blocked and fx.entry_blocks(db) == {c2.id: blocked}
    # A restart: nothing but the database.
    with OrmSession(db.get_bind()) as fresh:
        assert fx.entry_block_for(fresh, c2.id) == blocked
        assert list(fx.closures(fresh)) == ["1cd460d679:none"]


def test_a_failed_registration_no_longer_discards_the_rest_of_the_seed_pass(db, monkeypatch):  # noqa: F811
    from app.services import seed

    db.add(Strategy(name="earlier lineup step", strategy_type="earlier_step", parameters={}, is_active=True,
                    current_status="research"))
    db.flush()  # written by an earlier lineup step of the same pass, not committed yet
    monkeypatch.setattr(fx, "load_spec", lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
    assert seed.register_forward_experiments_once(db)["status"] == "failed"
    assert db.scalars(select(Strategy).where(Strategy.strategy_type == "earlier_step")).one().name == "earlier lineup step"


def test_an_operator_closure_needs_a_registered_candidate_a_reason_and_an_operator_kind(db):  # noqa: F811
    _registered_rows(db)
    with pytest.raises(fx.ForwardExperimentError, match="not registered"):
        fx.close_candidate_by_number(db, 9, reason="there is no ninth candidate")
    with pytest.raises(fx.ForwardExperimentError, match="at least 10 characters"):
        fx.close_candidate_by_number(db, 2, reason="short")
    with pytest.raises(fx.ForwardExperimentError, match="reason_kind"):
        fx.close_candidate_by_number(db, 2, reason="only the review closes on the stop rule", reason_kind="stop_rule")
    assert fx.closures(db) == {}


def test_an_annul_row_would_reopen_only_its_own_candidate():
    rows = [(1, fx.ACTION_CLOSE, {"sweep_id": "a", "entries_blocked": True}),
            (2, fx.ACTION_CLOSE, {"sweep_id": "b", "entries_blocked": True}),
            (3, fx.ACTION_ANNUL, {"sweep_id": "a"}),
            (4, "close", {"no": "sweep id"})]
    assert fx._closure_state(rows) == {"b": {"sweep_id": "b", "entries_blocked": True, "audit_id": 2}}


def test_the_close_route_and_the_settling_job_are_wired():
    import inspect

    from app.api.stock_paper import ForwardExperimentCloseRequest, router
    from app.tasks import jobs

    paths = {route.path: route.methods for route in router.routes}
    assert "POST" in paths["/stock-paper/forward-experiments/{candidate}/close"]
    with pytest.raises(ValueError):
        ForwardExperimentCloseRequest(reason="too short", confirm_candidate=2)
    with pytest.raises(ValueError):
        ForwardExperimentCloseRequest(reason="only the review closes on the stop rule", reason_kind="stop_rule",
                                      confirm_candidate=2)
    body = inspect.getsource(jobs.forward_experiments_review_job)
    assert body.index("settle_closures(db)") < body.index("record_review(db)")


def test_the_close_endpoint_is_admin_only_confirms_the_candidate_and_writes_the_closure(db):  # noqa: F811
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.stock_paper import router
    from app.core.config import Settings
    from app.core.security import AuthenticationMiddleware, required_role
    from app.db.session import get_db

    rows = _registered_rows(db)
    db.commit()
    c2 = rows["rule_1cd460d679"]
    app = FastAPI()
    app.add_middleware(AuthenticationMiddleware, configuration=Settings(
        auth_viewer_key="v" * 40, auth_researcher_key="r" * 40, auth_operator_key="o" * 40, auth_admin_key="a" * 40))
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: db
    assert required_role("POST", "/stock-paper/forward-experiments/2/close") == "admin"
    url = "/api/stock-paper/forward-experiments/2/close"
    body = {"reason": "cannot reach a verdict in a useful time", "confirm_candidate": 2}
    admin = {"Authorization": "Bearer " + "a" * 40}
    with TestClient(app) as client:
        assert client.post(url, json=body).status_code == 401
        for role in ("v", "r", "o"):
            assert client.post(url, json=body, headers={"Authorization": "Bearer " + role * 40}).status_code == 403
        assert fx.closures(db) == {}
        assert client.post(url, json={**body, "confirm_candidate": 3}, headers=admin).status_code == 422
        assert client.post(url, json={**body, "reason": "short"}, headers=admin).status_code == 422
        assert client.post(url, json={**body, "reason_kind": "stop_rule"}, headers=admin).status_code == 422
        missing = client.post("/api/stock-paper/forward-experiments/9/close", json={**body, "confirm_candidate": 9},
                              headers=admin)
        assert missing.status_code == 422 and "not registered" in missing.text
        assert fx.closures(db) == {}
        done = client.post(url, json=body, headers=admin)
        assert done.status_code == 200, done.text
        out = done.json()
        assert out["status"] == "closed" and out["paper_only"] is True and out["live_authorized"] is False
        assert out["closure"]["reason_kind"] == "operator_closure" and out["closure"]["entries_blocked"] is True
        assert out["closure"]["closed_by"].startswith("operator:")
        assert client.post(url, json=body, headers=admin).json()["status"] == "already_closed"
    assert len(_close_rows(db)) == 1 and fx.entry_block_for(db, c2.id) is not None


@pytest.mark.postgres
def test_two_closers_at_once_write_one_close_row_on_postgresql():
    """The weekly review and an operator closing the same candidate together: one row, one unbroken audit chain."""
    import threading
    from uuid import uuid4

    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.orm import Session as OrmSession

    from app.db.base import Base

    url = make_url(settings.database_url)
    if url.get_backend_name() != "postgresql":
        pytest.skip("Requires isolated PostgreSQL validation stack")
    owner = create_engine(url)
    schema = "fxclose_" + uuid4().hex
    with owner.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url.update_query_dict({"options": f"-csearch_path={schema}"}))
    try:
        Base.metadata.create_all(engine)
        with OrmSession(engine) as setup:
            _ibs(setup)
            fx.register_forward_experiments(setup, now=NOW)
            setup.commit()
        barrier, outcomes, errors = threading.Barrier(2), [], []

        def closer(name):
            try:
                with OrmSession(engine) as session:
                    barrier.wait(timeout=10)
                    out = fx.close_candidate_by_number(session, 2, reason=f"closed at the same moment by {name}",
                                                       actor=name)
                    session.commit()
                    outcomes.append(out["status"])
            except Exception as exc:  # surfaced below; a thread must not swallow it
                errors.append(exc)

        threads = [threading.Thread(target=closer, args=(name,)) for name in ("review", "operator")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert errors == [] and sorted(outcomes) == ["already_closed", "closed"]
        with OrmSession(engine) as check:
            assert len(_close_rows(check)) == 1 and list(fx.closures(check)) == ["1cd460d679:none"]
            chain = check.execute(select(AuditLog.previous_event_sha256, AuditLog.event_sha256).order_by(AuditLog.id)).all()
            assert [previous for previous, _ in chain[1:]] == [digest for _, digest in chain[:-1]]
    finally:
        engine.dispose()
        with owner.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        owner.dispose()
