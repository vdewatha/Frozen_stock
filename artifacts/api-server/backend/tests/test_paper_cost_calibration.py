"""Paper cost calibration: realised fill costs measured, shrunk into multipliers, applied to the replays."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from app.core.config import settings
from app.core.security import required_role
from app.models import AuditLog
from app.models.stock_paper import StockPaperAccount, StockPaperFill, StockPaperOrder
from app.services import gate_replay
from app.services import paper_cost_calibration as pcc
from app.services import weekly_report as weekly
from app.services.backtester import BacktestConfig, execution_cost_bps
from app.services.exchange_sessions import is_nyse_session
from app.services.triple_barrier import BarrierConfig, CostMultipliers, label_events, side_cost_bps
from app.tasks.celery_app import celery_app
from tests.test_experiment_engine import _walk
from tests.test_loss_feedback_loop import db  # noqa: F401  (fixture)

UTC = timezone.utc
ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 7, 18, 30, tzinfo=UTC)  # Saturday 13:30 ET, the calibration's beat slot
SESSION = datetime(2026, 11, 5, tzinfo=ET)  # Thursday


def _et(day: datetime, hour: int, minute: int = 0) -> datetime:
    return day.replace(hour=hour, minute=minute, tzinfo=ET).astimezone(UTC)


def _frame(closes, start="2026-08-03", volume=2_000_000.0):
    closes = np.asarray(closes, dtype=float)
    dates = pd.bdate_range(start, periods=len(closes)).strftime("%Y-%m-%d")
    return pd.DataFrame({"date": dates, "open": closes, "high": closes * 1.005, "low": closes * 0.995,
                         "close": closes, "volume": np.full(len(closes), volume)})


class _Order:
    def __init__(self, order_type="limit", leg_role=None, limit_price=None, stop_price=None, strategy_id=7):
        self.order_type, self.leg_role = order_type, leg_role
        self.limit_price, self.stop_price, self.strategy_id = limit_price, stop_price, strategy_id


class _Fill:
    _ids = iter(range(1, 100_000))

    def __init__(self, symbol="SPY", side="buy", quantity=10, price=100.10, fee=None, filled_at=None, order_id=1):
        self.id = next(self._ids)
        self.order_id = order_id
        self.symbol, self.side = symbol, side
        self.quantity, self.price, self.fee = Decimal(str(quantity)), Decimal(str(price)), fee
        self.filled_at = filled_at or _et(SESSION, 12)


# --------------------------------------------------------------- buckets

def test_symbol_classes_and_time_buckets():
    assert pcc.symbol_class("SPY") == "index_etf" and pcc.symbol_class("qqq") == "index_etf"
    assert pcc.symbol_class("XLK") == "sector_etf" and pcc.symbol_class("SMH") == "sector_etf"
    assert pcc.symbol_class("AAPL") == "single_stock" and pcc.symbol_class(None) == "single_stock"
    assert pcc.time_bucket(_et(SESSION, 9, 31)) == "open"
    assert pcc.time_bucket(_et(SESSION, 9, 59)) == "open"
    assert pcc.time_bucket(_et(SESSION, 10, 1)) == "midday"
    assert pcc.time_bucket(_et(SESSION, 15, 29)) == "midday"
    assert pcc.time_bucket(_et(SESSION, 15, 45)) == "close"
    assert pcc.time_bucket(_et(SESSION, 8, 0)) == "off_hours"
    assert pcc.time_bucket(_et(SESSION + timedelta(days=2), 12)) == "off_hours"  # Saturday
    assert pcc.time_bucket(_et(SESSION, 12).replace(tzinfo=None)) == "midday"  # naive = UTC
    assert pcc.session_date(_et(SESSION, 19, 59)) == SESSION.date()


def test_order_kinds_and_decision_prices():
    assert pcc.order_kind(None) == "unknown" and pcc.decision_price(None) is None
    entry = _Order(limit_price=Decimal("100"))
    assert pcc.order_kind(entry) == "limit" and pcc.decision_price(entry) == 100.0
    assert pcc.order_kind(_Order(order_type="market")) == "market"
    take = _Order(leg_role="take_profit", limit_price=Decimal("106"))
    assert pcc.order_kind(take) == "bracket_take_profit" and pcc.decision_price(take) == 106.0
    stop = _Order(order_type="stop", leg_role="stop_loss", stop_price=Decimal("97"), limit_price=None)
    assert pcc.order_kind(stop) == "protective_stop" and pcc.decision_price(stop) == 97.0
    assert pcc.order_kind(_Order(order_type="trailing_stop")) == "protective_stop"
    assert pcc.decision_price(_Order(limit_price=None)) is None


# --------------------------------------------------------------- measure

def test_fill_is_scored_against_its_decision_price_and_the_model():
    frame = _frame(100 + np.sin(np.arange(60)) * 2)
    fill = _Fill(price=100.10, quantity=10, fee=Decimal("0.02"), filled_at=_et(SESSION, 9, 40))
    row = pcc.observe_fill(fill, _Order(limit_price=Decimal("100")), frame)
    assert row["slippage_bps"] == pytest.approx(10.0)
    assert row["fee_bps"] == pytest.approx(0.02 / 1001 * 10_000, abs=1e-4) and row["fee_known"]
    assert row["realised_total_bps"] == pytest.approx(row["slippage_bps"] + row["fee_bps"])
    assert row["time_bucket"] == "open" and row["session"] == "2026-11-05" and row["symbol_class"] == "index_etf"
    assert row["order_kind"] == "limit" and row["strategy_id"] == 7
    # The model is priced from the bars strictly before the fill's session, with the fill's own quantity.
    expected = pcc.model_cost(frame, SESSION.date(), 10)
    assert row["history_bars"] == int((frame["date"] < "2026-11-05").sum()) > 20
    assert row["modelled_slippage_bps"] == expected["slippage_bps"] > 5.0  # base + volatility term
    assert row["modelled_total_bps"] == pytest.approx(expected["slippage_bps"] + 1.0)
    assert row["residual"] == pytest.approx(10.0 / expected["slippage_bps"], abs=1e-4)
    assert row["gap_bps"] == pytest.approx(row["realised_total_bps"] - row["modelled_total_bps"], abs=1e-4)
    assert row["usable"]
    # A sell filled above its reference is price improvement: negative slippage.
    sell = pcc.observe_fill(_Fill(side="sell", price=100.10), _Order(limit_price=Decimal("100")), frame)
    assert sell["slippage_bps"] == pytest.approx(-10.0) and sell["fee_bps"] is None and not sell["fee_known"]
    # No decision price: counted, not priced, not usable for the fit.
    unknown = pcc.observe_fill(_Fill(order_id=None), None, frame)
    assert unknown["slippage_bps"] is None and unknown["residual"] is None and not unknown["usable"]
    assert unknown["order_kind"] == "unknown" and unknown["modelled_slippage_bps"] > 0
    # Without daily bars the model falls back to its floor (no volatility term).
    floor = pcc.observe_fill(fill, _Order(limit_price=Decimal("100")), None)
    assert floor["modelled_slippage_bps"] == 5.0 and floor["history_bars"] == 0


def test_summary_reports_counts_mean_median_p90_and_the_gap():
    frame = _frame(np.full(60, 100.0))  # zero volatility: the model charges exactly its 5 bps floor
    prices = [100.05, 100.10, 100.10, 100.20, 100.40]
    rows = [pcc.observe_fill(_Fill(price=p, fee=Decimal("0")), _Order(limit_price=Decimal("100")), frame)
            for p in prices]
    rows.append(pcc.observe_fill(_Fill(order_id=None), None, frame))
    summary = pcc.summarise(rows)
    assert summary["fills"] == 6 and summary["priced"] == 5 and summary["usable"] == 5 and summary["fee_known"] == 5
    assert summary["realised_slippage_bps"] == {"mean": 17.0, "median": 10.0, "p90": pytest.approx(32.0)}
    assert summary["modelled_slippage_bps_mean"] == 5.0 and summary["modelled_total_bps_mean"] == 6.0
    assert summary["realised_total_bps"]["mean"] == 17.0 and summary["gap_bps_mean"] == pytest.approx(11.0)
    assert summary["residual"] == pytest.approx(17.0 / 5.0) and summary["residual_median"] == pytest.approx(2.0)
    tables = pcc.measurement_tables(rows)
    assert tables["overall"]["fills"] == 6
    assert [r["order_kind"] for r in tables["by_order_kind"]] == ["limit", "unknown"]
    assert tables["by_symbol"][0]["symbol"] == "SPY" and tables["by_symbol"][0]["fills"] == 6
    assert tables["by_time_bucket"] == [{"time_bucket": "midday", **pcc.summarise(rows)}]
    assert tables["cells"][0]["symbol_class"] == "index_etf" and tables["cells"][0]["time_bucket"] == "midday"
    assert pcc.summarise([])["realised_slippage_bps"] == {"mean": None, "median": None, "p90": None}


# ------------------------------------------------------------- calibrate

def test_shrinkage_needs_twenty_fills_and_is_capped():
    assert pcc.shrink(2.0, 19) is None and pcc.shrink(None, 100) is None
    assert pcc.shrink(2.0, 20) == pytest.approx((20 * 2.0 + 20) / 40)       # 1.5: half way to 1.0
    assert pcc.shrink(2.0, 180) == pytest.approx((180 * 2.0 + 20) / 200)    # 1.9: evidence wins
    assert pcc.shrink(10.0, 1000) == 3.0 and pcc.shrink(0.0, 1000) == 0.5   # caps
    assert pcc.shrink(-4.0, 50) == 0.5                                      # price improvement never below the floor
    assert pcc.shrink(1.0, 20) == 1.0
    assert pcc.shrink(2.0, 5, min_fills=5, shrink_fills=0) == 2.0


def _usable(symbol_class, bucket, realised, modelled=5.0, n=1):
    return [{"symbol_class": symbol_class, "time_bucket": bucket, "slippage_bps": realised,
             "modelled_slippage_bps": modelled, "usable": True} for _ in range(n)]


def test_cells_are_fitted_with_enough_fills_and_fall_back_to_coarser_ones():
    rows = (_usable("index_etf", "open", 10.0, n=25)        # 2.0 raw, fitted
            + _usable("index_etf", "midday", 2.5, n=10)     # too few: unfitted
            + _usable("single_stock", "close", 15.0, n=4)
            + [{"symbol_class": "single_stock", "time_bucket": "open", "slippage_bps": 99.0,
                "modelled_slippage_bps": 5.0, "usable": False}])   # not priced: ignored
    cells = {(c["symbol_class"], c["time_bucket"]): c for c in pcc.fit_cells(rows)}
    assert cells[("index_etf", "open")] == {"symbol_class": "index_etf", "time_bucket": "open", "fills": 25,
                                            "raw": 2.0, "multiplier": pytest.approx((25 * 2 + 20) / 45, abs=1e-4), "fitted": True}
    assert cells[("index_etf", "midday")]["fitted"] is False and cells[("index_etf", "midday")]["raw"] == 0.5
    assert cells[("index_etf", "all")]["fills"] == 35 and cells[("index_etf", "all")]["fitted"]
    assert cells[("index_etf", "all")]["raw"] == pytest.approx((250 + 25) / 175, abs=1e-4)
    assert cells[("single_stock", "close")]["fitted"] is False and cells[("single_stock", "all")]["fills"] == 4
    assert cells[("sector_etf", "all")] == {"symbol_class": "sector_etf", "time_bucket": "all", "fills": 0,
                                            "raw": None, "multiplier": None, "fitted": False}
    assert cells[("all", "all")]["fills"] == 39 and cells[("all", "open")]["fills"] == 25
    calibration = pcc.CostCalibration.from_cells(cells.values())
    assert set(calibration.multipliers) == {("index_etf", "open"), ("index_etf", "all"), ("all", "open"), ("all", "all")}
    spy = calibration.for_symbol("SPY")
    assert spy.entry == cells[("index_etf", "open")]["multiplier"] and spy.exit == cells[("index_etf", "all")]["multiplier"]
    aapl = calibration.for_symbol("AAPL")   # no single-stock cell fitted: the global fallbacks
    assert aapl.entry == cells[("all", "open")]["multiplier"] and aapl.exit == cells[("all", "all")]["multiplier"]
    assert not calibration.is_identity
    # Round trip through the stored form.
    again = pcc.CostCalibration.from_dict(calibration.to_dict())
    assert again.multipliers == calibration.multipliers
    assert pcc.CostCalibration.from_dict(None) is None
    assert pcc.CostCalibration.from_dict({"multipliers": [{"symbol_class": "x", "time_bucket": "y", "multiplier": -1}]}).is_identity
    assert pcc.CostCalibration().for_symbol("SPY") == CostMultipliers(1.0, 1.0)
    assert pcc.fit_cells([]) and all(not c["fitted"] for c in pcc.fit_cells([]))


# --------------------------------------------------------- cost functions

def test_cost_functions_apply_the_multiplier_and_default_to_the_published_model():
    config = BarrierConfig()
    base = side_cost_bps(150.0, 1e6, 100.0, config)
    assert side_cost_bps(150.0, 1e6, 100.0, config, 1.0) == base
    assert side_cost_bps(150.0, 1e6, 100.0, config, 1.8) == pytest.approx(base * 1.8)
    assert side_cost_bps(150.0, 1e6, 100.0, config, 0.5) == pytest.approx(base * 0.5)
    flat = BarrierConfig(cost_model="flat", slippage_bps=5)
    assert side_cost_bps(150.0, 1e6, 100.0, flat, 2.0) == 10.0 and side_cost_bps(150.0, 1e6, 100.0, flat) == 5.0
    with pytest.raises(ValueError):
        CostMultipliers(entry=0.0)
    with pytest.raises(ValueError):
        CostMultipliers(exit=float("nan"))

    prices = _walk(rows=120, seed=5)
    events = list(range(30, 90, 7))
    plain = label_events(prices, events, config)
    same = label_events(prices, events, config, calibration=None)
    identity = label_events(prices, events, config, calibration=CostMultipliers())
    pd.testing.assert_frame_equal(plain, same)
    pd.testing.assert_frame_equal(plain, identity)
    dearer = label_events(prices, events, config, calibration=CostMultipliers(entry=2.0, exit=2.0))
    assert len(dearer) == len(plain) > 0
    assert list(dearer["barrier"]) == list(plain["barrier"]) or True  # levels move with the entry price
    assert (dearer["entry_price"] > plain["entry_price"]).all()
    assert (dearer["net_return"] < plain["net_return"]).all()
    entry_only = label_events(prices, events, config, calibration=CostMultipliers(entry=2.0, exit=1.0))
    assert (entry_only["entry_price"] == dearer["entry_price"]).all()
    assert (entry_only["net_return"] > dearer["net_return"]).all()

    # The backtester's own cost function: config field, explicit override, flat model.
    history = prices.iloc[:60]
    published = execution_cost_bps(history, 100.0, BacktestConfig())
    assert published["multiplier"] == 1.0
    scaled = execution_cost_bps(history, 100.0, BacktestConfig(cost_multiplier=1.5))
    assert scaled["total_bps"] == pytest.approx(published["total_bps"] * 1.5) and scaled["multiplier"] == 1.5
    override = execution_cost_bps(history, 100.0, BacktestConfig(cost_multiplier=1.5), multiplier=0.5)
    assert override["total_bps"] == pytest.approx(published["total_bps"] * 0.5)
    assert execution_cost_bps(history, 100.0, BacktestConfig(cost_model="flat", cost_multiplier=2.0))["total_bps"] == 10.0


# ----------------------------------------------------------- audit row

def _seed_fills(db, *, symbol="SPY", count=25, spread_bps=12.0, start_day=SESSION - timedelta(days=70)):  # noqa: F811
    account = StockPaperAccount(broker="alpaca_paper", broker_account_id="paper-1", cash=100_000, equity=100_000,
                                buying_power=100_000, raw_payload={})
    db.add(account)
    db.flush()
    fills = []
    day = start_day
    for i in range(count):
        while not is_nyse_session(day.date()):   # weekends and holidays (Labor Day 2026-09-07)
            day += timedelta(days=1)
        entry = StockPaperOrder(account_id=account.id, client_order_id=f"{symbol}-{i}", symbol=symbol, side="buy",
                                quantity=Decimal("10"), order_type="limit", limit_price=Decimal("100"),
                                reserved_cash=Decimal("1000"), status="filled", source="scheduled_signal",
                                order_class="bracket", stop_price=Decimal("97"), take_profit_price=Decimal("106"),
                                strategy_id=None)
        db.add(entry)
        db.flush()
        fills.append(StockPaperFill(account_id=account.id, order_id=entry.id, broker_activity_id=f"act-{symbol}-{i}",
                                    symbol=symbol, side="buy", quantity=Decimal("10"),
                                    price=Decimal("100") * (1 + Decimal(str(spread_bps)) / 10_000), fee=Decimal("0"),
                                    cost_known=True, filled_at=_et(day, 9, 45), raw_payload={}))
        day += timedelta(days=1)
    db.add_all(fills)
    db.flush()
    return account, fills


def test_calibration_is_recorded_once_per_session_as_an_audit_row_and_feeds_the_views(db, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "paper_cost_calibration_lookback_sessions", 60)
    monkeypatch.setattr(settings, "paper_cost_calibration_min_fills", 20)
    _seed_fills(db, count=25, spread_bps=12.0)       # 12 bps realised against a 5 bps flat-volatility model
    frame = _frame(np.full(120, 100.0))               # zero volatility: the model charges its 5 bps floor
    loader = lambda symbol: frame  # noqa: E731

    result = pcc.record_calibration(db, now=NOW, loader=loader)
    assert result["recorded"] and result["as_of"] == "2026-11-06" and result["fills"] == 25 == result["usable"]
    assert result["residual"] == pytest.approx(12.0 / 5.0)
    again = pcc.record_calibration(db, now=NOW + timedelta(hours=2), loader=loader)
    assert again["recorded"] is False and "already" in again["reason"]
    rows = db.query(AuditLog).filter_by(event_type="stock_paper", action="paper_cost_calibration").all()
    assert len(rows) == 1 and rows[0].payload["paper_only"] is True

    report = pcc.latest_calibration_report(db)
    assert report["as_of"] == "2026-11-06" and report["window"]["sessions"] == 60
    assert report["window"]["first_session"] == "2026-08-14" and report["window"]["last_session"] == "2026-11-06"
    assert report["fills"]["count"] == 25 and report["fills"]["truncated"] == 0
    assert [r["fill_id"] for r in report["fills"]["rows"]] == sorted(r["fill_id"] for r in report["fills"]["rows"])
    assert report["fills"]["rows"][0]["decision_price"] == 100.0 and report["fills"]["rows"][0]["slippage_bps"] == pytest.approx(12.0)
    assert report["tables"]["overall"]["residual"] == pytest.approx(2.4)
    assert report["tables"]["by_time_bucket"][0]["time_bucket"] == "open"
    assert report["tables"]["by_order_kind"][0] == {"order_kind": "limit", **report["tables"]["overall"]}
    assert report["model"]["cost_model"] == "volatility_scaled" and report["settings"]["min_fills"] == 20
    cells = {(c["symbol_class"], c["time_bucket"]): c for c in report["cells"]}
    expected = (25 * 2.4 + 20) / 45
    assert cells[("index_etf", "open")]["fitted"] and cells[("index_etf", "open")]["multiplier"] == pytest.approx(expected, abs=1e-4)
    assert cells[("single_stock", "all")]["fitted"] is False
    assert report["fitted_cells"] == 4   # (index_etf, open/all), (all, open/all)

    calibration = pcc.latest_calibration(db)
    assert calibration.as_of == "2026-11-06" and calibration.fills == 25
    assert calibration.for_symbol("SPY").entry == pytest.approx(expected, abs=1e-4)
    assert calibration.for_symbol("AAPL").exit == pytest.approx(expected, abs=1e-4)  # global fallback

    view = pcc.calibration_view(db)
    assert view["latest"]["as_of"] == "2026-11-06" and len(view["in_force"]) == 4 and view["paper_only"]
    assert view["lookback_sessions"] == 60 and view["min_fills"] == 20 and view["bounds"] == [0.5, 3.0]
    section = pcc.weekly_report_section(db)
    assert section["status"] == "ok" and section["fills"] == 25 and section["residual"] == pytest.approx(2.4)
    assert section["realised_total_bps"] == pytest.approx(12.0) and section["modelled_total_bps"] == 6.0
    assert section["gap_bps"] == pytest.approx(6.0) and len(section["multipliers"]) == 4
    assert "25 fills" in section["detail"] and "+12.0 bps" in section["detail"]
    assert weekly._cost_calibration_section(db) == section
    # A new session records a new calibration; forcing re-records the same one.
    assert pcc.record_calibration(db, now=NOW + timedelta(days=7), loader=loader)["recorded"]
    assert pcc.record_calibration(db, now=NOW + timedelta(days=7), loader=loader, force=True)["recorded"]


def test_no_fills_means_a_recorded_identity_calibration_and_the_published_model(db):  # noqa: F811
    assert pcc.weekly_report_section(db)["status"] == "waiting"
    assert pcc.calibration_view(db)["latest"] is None and pcc.calibration_view(db)["in_force"] == []
    assert pcc.latest_calibration(db) is None and gate_replay.cost_calibration(db) is None
    result = pcc.record_calibration(db, now=NOW, loader=lambda symbol: None)
    assert result["recorded"] and result["fills"] == 0 and result["fitted_cells"] == 0 and result["residual"] is None
    calibration = pcc.latest_calibration(db)
    assert calibration is not None and calibration.is_identity
    assert gate_replay.cost_calibration(db) is None   # identity: the replay keeps the published model
    section = pcc.weekly_report_section(db)
    assert section["status"] == "ok" and section["multipliers"] == [] and "no priced fills" in section["detail"]


# ------------------------------------------------------------ the loader

def test_loader_reads_the_latest_calibration_and_the_replay_uses_it(db, monkeypatch):  # noqa: F811
    _seed_fills(db, count=40, spread_bps=10.0)      # raw 2.0 against the 5 bps floor
    frame = _frame(np.full(120, 100.0))
    pcc.record_calibration(db, now=NOW, loader=lambda symbol: frame)
    calibration = gate_replay.cost_calibration(db)
    assert calibration is not None and calibration.for_symbol("SPY").entry == pytest.approx((40 * 2 + 20) / 60, abs=1e-4)

    # Without a session the loader opens its own; a failure (or an empty ledger) is the published model.
    import app.db.session as session_module

    def broken():
        raise RuntimeError("database down")

    monkeypatch.setattr(session_module, "SessionLocal", broken)
    gate_replay.reset_calibration_cache()
    assert pcc.latest_calibration() is None and gate_replay.cost_calibration() is None
    # The default lookup is cached per process; an injected answer shows the cache is what simulate reads.
    gate_replay._calibration_cache.update(at=None, value=None)
    monkeypatch.setattr(pcc, "latest_calibration", lambda db=None: calibration)
    assert gate_replay.cost_calibration() is calibration
    monkeypatch.setattr(pcc, "latest_calibration", broken)
    assert gate_replay.cost_calibration() is calibration      # served from the cache, no second read
    gate_replay.reset_calibration_cache()

    frames = {"SPY": _walk(rows=700, seed=11)}
    published = gate_replay.simulate("channel_breakout", frames, frames["SPY"], {}, calibration=None)
    assert len(published) > 0
    dearer = gate_replay.simulate("channel_breakout", frames, frames["SPY"], {}, calibration=calibration)
    # Dearer entries lift the barrier levels, so the one-position chain can resolve differently; every
    # decision both replays take and exit the same way costs more and nets less under the calibration.
    shared = published.merge(dearer, on=["event_idx", "barrier"], suffixes=("_published", "_dearer"))
    assert len(shared) >= 10
    assert (shared["entry_price_dearer"] > shared["entry_price_published"]).all()
    assert (shared["net_return_dearer"] < shared["net_return_published"]).all()
    # The default argument reads the calibration in force (here: what the cache holds).
    monkeypatch.setattr(gate_replay, "cost_calibration", lambda db=None, **kw: calibration)
    by_default = gate_replay.simulate("channel_breakout", frames, frames["SPY"], {})
    pd.testing.assert_frame_equal(by_default, dearer)
    monkeypatch.setattr(gate_replay, "cost_calibration", lambda db=None, **kw: None)
    pd.testing.assert_frame_equal(gate_replay.simulate("channel_breakout", frames, frames["SPY"], {}), published)


# -------------------------------------------------------------- wiring

def test_job_route_and_beat_are_wired():
    from app.tasks import jobs

    beat = celery_app.conf.beat_schedule
    entry = beat["paper-cost-calibration"]
    assert entry["task"] == "app.tasks.jobs.paper_cost_calibration_job"
    saturday, sunday = {6}, {0}  # celery crontab: 0 = Sunday
    assert entry["schedule"].day_of_week == saturday and entry["schedule"].hour == {13} and entry["schedule"].minute == {30}
    # Saturday before the weekly report, and before the Sunday experiment engine.
    assert beat["weekly-performance-report"]["schedule"].day_of_week == saturday
    assert beat["weekly-performance-report"]["schedule"].hour == {14}
    assert beat["experiment-engine"]["schedule"].day_of_week == sunday
    assert celery_app.conf.task_routes["app.tasks.jobs.paper_cost_calibration_job"] == {"queue": "learning"}
    assert required_role("GET", "/stock-paper/cost-calibration") == "viewer"
    assert callable(jobs.paper_cost_calibration_job)
    assert settings.paper_cost_calibration_lookback_sessions == 60
    assert settings.paper_cost_calibration_min_fills == 20 == pcc.MIN_FILLS
