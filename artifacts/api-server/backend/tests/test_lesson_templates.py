"""The last three diagnosed lessons get a protection, a replay and a template: sector gate, breadth gate, entry cap."""
from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models import AuditLog
from app.services import book_replay
from app.services import experiment_engine as engine
from app.services import gate_replay
from app.services import market_gauges
from app.services import seed
from app.services.entry_protections import (
    EntryProtections,
    ProtectionConfig,
    entry_cap_block,
    load_entry_protections,
    protection_config,
)
from app.services.loss_diagnosis import CONTRIBUTING_PROPOSALS, PROPOSALS
from app.services.market_gate import (
    SECTOR_GATE_KEY,
    gate_window,
    sector_gate_window,
    sector_state,
    sector_symbol,
)
from app.services.point_in_time_universe import Membership
from app.services.risk_settings import get_active_risk_rule
from app.services.stock_paper_autotrader import run_stock_paper_signal_cycle
from tests.test_entry_protections import _account
from tests.test_experiment_engine import _attribution, _lesson, _window
from tests.test_loss_feedback_loop import _strategy, db  # noqa: F401  (fixture)
from tests.test_meta_label import _walk
from tests.test_stock_paper_autotrader import _ModelDb, signal_source

UTC = timezone.utc
NOW = datetime(2026, 11, 6, 15, 0, tzinfo=UTC)          # Friday 10:00 ET, inside the session
SUNDAY = datetime(2026, 11, 8, 17, 0, tzinfo=UTC)       # the engine's weekly run
ACCOUNT = SimpleNamespace(id=1, equity=Decimal("100000"), last_equity=Decimal("100000"))


def _frame(closes, start="2026-01-02"):
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame({"date": pd.bdate_range(start, periods=len(closes)).strftime("%Y-%m-%d"),
                         "open": closes, "high": closes * 1.005, "low": closes * 0.995, "close": closes,
                         "volume": np.full(len(closes), 2_000_000.0)})


FALLING = _frame(np.linspace(120, 100, 60))
RISING = _frame(np.linspace(100, 120, 60))


# ------------------------------------------------------------- sector gate

def test_sector_symbol_follows_the_attribution_mapping_and_windows_parse():
    assert sector_symbol("AAPL") == "XLK" and sector_symbol("jpm") == "XLF" and sector_symbol("GOOGL") == "XLC"
    # Index ETFs, the sector ETFs themselves, the inverse hedge and unmapped names have no sector ETF.
    assert sector_symbol("SPY") is None and sector_symbol("XLK") is None and sector_symbol("SH") is None
    assert sector_symbol("ZZZZ") is None
    assert sector_gate_window({SECTOR_GATE_KEY: 20}) == 20 and sector_gate_window({SECTOR_GATE_KEY: "50"}) == 50
    assert sector_gate_window({SECTOR_GATE_KEY: -1}) == 0 and sector_gate_window({"market_gate_sma": 20}) == 0
    assert gate_window({SECTOR_GATE_KEY: 20}) == 0  # the two gates are separate keys


def test_sector_state_and_replay_mask():
    assert sector_state(RISING, 20, "XLK")["allowed"] is True
    blocked = sector_state(FALLING, 20, "XLK")
    assert blocked["allowed"] is False and "XLK" in blocked["reason"] and "20-day average" in blocked["reason"]
    assert sector_state(None, 20, "XLK")["allowed"] is False               # fail closed
    assert sector_state(_frame([100.0] * 5), 20, "XLK")["allowed"] is False  # too short: fail closed
    assert sector_state(FALLING, 0, "XLK")["allowed"] is True

    frame = _frame(np.full(6, 100.0))
    etf = pd.DataFrame({"date": frame["date"], "close": [10.0, 10, 10, 11, 9, 12]})
    np.testing.assert_array_equal(gate_replay.sector_gate_on(frame, etf, 3), [0, 0, 0, 1, 0, 1])
    assert gate_replay.sector_gate_on(frame, None, 3).sum() == 0              # fails closed, as live
    assert gate_replay.sector_gate_on(frame, None, 0).all()
    assert gate_replay.setup_from_parameters({SECTOR_GATE_KEY: "20"})[SECTOR_GATE_KEY] == 20


def test_replay_applies_the_sector_gate_only_to_symbols_with_a_sector_etf():
    frames = {"SPY": _walk(rows=500, seed=3, start="2024-01-02"), "AAPL": _walk(rows=500, seed=4, start="2024-01-02"),
              "XLK": _walk(rows=500, seed=5, start="2024-01-02")}
    from app.services import meta_label_primary as primary

    replays = {s: primary.replay_signals(f) for s, f in frames.items()}
    free = gate_replay.simulate("channel_breakout", frames, frames["SPY"], {}, replays, calibration=None)
    # A 10000-day average never exists: the sector gate blocks every AAPL entry; SPY and XLK are untouched.
    gated = gate_replay.simulate("channel_breakout", frames, frames["SPY"], {SECTOR_GATE_KEY: 10_000}, replays,
                                 calibration=None)
    assert "AAPL" in set(free["symbol"]) and "AAPL" not in set(gated["symbol"])
    for symbol in ("SPY", "XLK"):
        assert (free["symbol"] == symbol).sum() == (gated["symbol"] == symbol).sum()
    # The ETF frames can also be supplied apart from the traded frames.
    apart = gate_replay.simulate("channel_breakout", {"AAPL": frames["AAPL"], "SPY": frames["SPY"]}, frames["SPY"],
                                 {SECTOR_GATE_KEY: 20}, replays, calibration=None, sectors={"XLK": frames["XLK"]})
    same = gate_replay.simulate("channel_breakout", frames, frames["SPY"], {SECTOR_GATE_KEY: 20}, replays,
                                calibration=None)
    assert (apart["symbol"] == "AAPL").sum() == (same["symbol"] == "AAPL").sum()


def _protections(db, **kwargs):  # noqa: F811
    return load_entry_protections(db, ACCOUNT, now=NOW, rules=kwargs.pop("rules", {}), type_by_id={},
                                  bounds_for=kwargs.pop("bounds_for", lambda _d: None), **kwargs)


def test_live_sector_gate_blocks_gated_strategies_and_fails_closed_per_etf(db):  # noqa: F811
    def etf_history(etf):
        if etf == "XLF":
            raise RuntimeError("feed down")
        return FALLING if etf == "XLK" else RISING

    protections = _protections(db, sector_gates={"macd_momentum": 20, "ibs_mean_reversion": 0},
                               symbols=["AAPL", "JPM", "AMZN", "SPY"], sector_frame=etf_history)
    assert protections.load_error is None
    block = protections.check("AAPL", "macd_momentum")
    assert block is not None and block["protection"] == "sector_gate"
    assert "XLK" in block["reason"] and "no new longs in AAPL while its sector falls" in block["reason"]
    unavailable = protections.check("JPM", "macd_momentum")             # XLF could not be loaded: fail closed
    assert unavailable is not None and "fail closed" in unavailable["reason"]
    assert protections.check("AMZN", "macd_momentum") is None            # XLY is rising
    assert protections.check("SPY", "macd_momentum") is None             # no sector ETF: never gated
    assert protections.check("AAPL", "ibs_mean_reversion") is None       # not gated
    assert protections.check("AAPL", "channel_breakout") is None
    summary = protections.summary()["sector_gate"]
    assert summary["gates"] == {"macd_momentum": 20} and summary["loaded"] == ["XLK", "XLY"]
    assert summary["unavailable"] == ["XLF"] and set(summary["states"]) == {"XLF:20", "XLK:20", "XLY:20"}
    # No gated strategy: no ETF is loaded at all.
    assert _protections(db, sector_gates={"macd_momentum": 0}, symbols=["AAPL"], sector_frame=etf_history).sector_history == {}


# ------------------------------------------------------------ breadth gate

def test_breadth_gate_parses_blocks_and_fails_open():
    assert market_gauges.breadth_gate_min({"breadth_gate_min": 50}) == 50.0
    assert market_gauges.breadth_gate_min({"breadth_gate_min": "x"}) == 0.0
    assert market_gauges.breadth_gate_min({"breadth_gate_min": -5}) == 0.0 and market_gauges.breadth_gate_min(None) == 0.0
    weak = {market_gauges.BREADTH: {"value": 35.0, "as_of": "2026-11-05", "fresh": True}}
    strong = {market_gauges.BREADTH: {"value": 62.0, "as_of": "2026-11-05", "fresh": True}}
    stale = {market_gauges.BREADTH: {"value": 35.0, "as_of": "2026-10-01", "fresh": False}}
    blocked = market_gauges.breadth_state(weak, 50)
    assert blocked["allowed"] is False and "35.0%" in blocked["reason"] and "below the 50% floor" in blocked["reason"]
    assert market_gauges.breadth_state(strong, 50)["allowed"] is True
    assert market_gauges.breadth_state(weak, 0)["allowed"] is True
    missing = market_gauges.breadth_state({}, 50)
    assert missing["allowed"] is True and missing["fail_open"] and "missing" in missing["reason"] \
        and "fails open" in missing["reason"]
    assert market_gauges.breadth_state(stale, 50)["fail_open"] and "stale" in market_gauges.breadth_state(stale, 50)["reason"]


def test_live_breadth_gate_blocks_only_gated_strategies(db):  # noqa: F811
    weak = {market_gauges.BREADTH: {"value": 35.0, "as_of": "2026-11-05", "fresh": True}}
    protections = _protections(db, breadth_gates={"channel_breakout": 50.0, "rsi2_pullback": 0}, gauges=weak)
    block = protections.check("AAPL", "channel_breakout")
    assert block is not None and block["protection"] == "breadth_gate" and "breadth is weak" in block["reason"]
    assert protections.check("AAPL", "rsi2_pullback") is None
    assert protections.summary()["breadth_gate"]["states"]["50"]["allowed"] is False
    # Missing gauge: the gate fails open and the state says so.
    open_ = _protections(db, breadth_gates={"channel_breakout": 50.0}, gauges={})
    assert open_.check("AAPL", "channel_breakout") is None
    assert open_.summary()["breadth_gate"]["states"]["50"]["fail_open"] is True


def test_breadth_history_counts_the_members_of_each_date(monkeypatch, tmp_path):
    monkeypatch.setattr(market_gauges, "MIN_PRICED_HISTORY", 1)
    dates = pd.bdate_range("2024-01-01", periods=260)
    cut = dates[230]
    membership = Membership.from_frame(pd.DataFrame({
        "ticker": ["UP", "DOWN", "LATE"], "start": [dates[0], dates[0], dates[0]],
        "end": [pd.NaT, cut, pd.NaT]}))
    frames = {"UP": _frame(np.linspace(50, 100, 260), "2024-01-01"),       # above its 200-day average
              "DOWN": _frame(np.linspace(100, 50, 260), "2024-01-01"),     # below it
              "LATE": _frame(np.full(30, 10.0), "2024-01-01")}             # too little history to count
    history = market_gauges.breadth_history(membership, loader=lambda t: frames[t], start="2024-01-01")
    assert list(history.columns) == ["date", "pct", "priced", "above"]
    by_date = history.set_index("date")
    first = dates[199].strftime("%Y-%m-%d")
    assert np.isnan(by_date["pct"].loc[dates[198].strftime("%Y-%m-%d")])          # no 200 closes yet
    assert by_date.loc[first, "priced"] == 2 and by_date.loc[first, "pct"] == 50.0
    # DOWN leaves the index on ``cut``: from that date only UP counts.
    assert by_date.loc[cut.strftime("%Y-%m-%d"), "priced"] == 1 and by_date.loc[cut.strftime("%Y-%m-%d"), "pct"] == 100.0
    series = market_gauges.breadth_series(history)
    assert series.index.is_monotonic_increasing and series.loc[pd.Timestamp(first)] == 50.0
    # The replay mask reads the decision bar's breadth; unknown readings fail open.
    frame = _frame(np.full(260, 1.0), "2024-01-01")
    allowed = gate_replay.breadth_gate_on(frame, series, 60.0)
    assert allowed[:199].all() and not allowed[199] and allowed[231]
    assert gate_replay.breadth_gate_on(frame, None, 60.0).all() and gate_replay.breadth_gate_on(frame, series, 0).all()
    # Caching: built once, read back while fresh, the old file kept when a rebuild fails.
    path = tmp_path / "breadth.csv"
    built = market_gauges.load_breadth_history(now=NOW, membership=membership, loader=lambda t: frames[t], path=path)
    assert path.exists() and len(built) == len(history)
    again = market_gauges.load_breadth_history(now=NOW, membership=membership,
                                               loader=lambda t: (_ for _ in ()).throw(RuntimeError("no")), path=path)
    assert len(again) == len(built)
    kept = market_gauges.load_breadth_history(now=NOW + timedelta(days=30), membership=membership,
                                              loader=lambda t: (_ for _ in ()).throw(RuntimeError("no")), path=path)
    assert len(kept) == len(built)
    with pytest.raises(FileNotFoundError):
        market_gauges.load_breadth_history(now=NOW, membership=membership,
                                           loader=lambda t: (_ for _ in ()).throw(RuntimeError("no")),
                                           path=tmp_path / "none.csv")


# --------------------------------------------------------------- entry cap

def test_entry_cap_counts_filled_entries_and_this_cycles_buys(db):  # noqa: F811
    from app.models.stock_paper import StockPaperAccount, StockPaperFill, StockPaperOrder

    config = protection_config({"max_new_entries_per_session": 2})
    assert config.max_new_entries_per_session == 2 and ProtectionConfig().max_new_entries_per_session == 0
    assert entry_cap_block(1, config) is None
    assert entry_cap_block(2, config)["protection"] == "entry_cap" and "cap 2" in entry_cap_block(2, config)["reason"]
    assert entry_cap_block(5, protection_config({"max_new_entries_per_session": 0})) is None

    account = StockPaperAccount(id=1, broker_account_id="paper", cash=Decimal("100000"), buying_power=Decimal("100000"),
                                equity=Decimal("100000"), status="reconciled", raw_payload={})
    db.add(account)
    db.flush()
    opened = NOW.replace(hour=14, minute=30)
    for order_id, symbol, at in ((11, "AAPL", opened + timedelta(minutes=5)), (12, "MSFT", opened + timedelta(minutes=9)),
                                 (13, "NVDA", opened - timedelta(days=1))):
        db.add(StockPaperOrder(id=order_id, account_id=1, client_order_id=f"sp-{order_id}", symbol=symbol, side="buy",
                               quantity=Decimal("1"), status="filled", source="scheduled_paper_signal",
                               created_at=at, updated_at=at))
        db.add(StockPaperFill(account_id=1, order_id=order_id, broker_activity_id=f"a{order_id}", symbol=symbol,
                              side="buy", quantity=Decimal("1"), price=Decimal("100"), filled_at=at, raw_payload={}))
    # A second partial fill of the same order is still one entry.
    db.add(StockPaperFill(account_id=1, order_id=11, broker_activity_id="a11b", symbol="AAPL", side="buy",
                          quantity=Decimal("1"), price=Decimal("100"), filled_at=opened + timedelta(minutes=6),
                          raw_payload={}))
    db.flush()
    bounds = (opened, opened + timedelta(hours=6, minutes=30))
    protections = load_entry_protections(db, account, now=NOW, rules={"max_new_entries_per_session": 3},
                                         type_by_id={}, bounds_for=lambda _d: bounds)
    assert protections.load_error is None and protections.session_entries == 2   # yesterday's NVDA does not count
    assert protections.check("TSLA", "macd_momentum") is None
    assert protections.record_entry("TSLA") == 3
    block = protections.check("AMZN", "macd_momentum")
    assert block is not None and block["protection"] == "entry_cap" and "3 new entries" in block["reason"]
    assert protections.summary()["entry_cap"] == {"cap": 3, "session_entries": 2, "cycle_entries": 1}
    capped = load_entry_protections(db, account, now=NOW, rules={"max_new_entries_per_session": 2},
                                    type_by_id={}, bounds_for=lambda _d: bounds)
    assert capped.check("TSLA", "macd_momentum")["protection"] == "entry_cap"
    off = load_entry_protections(db, account, now=NOW, rules={}, type_by_id={}, bounds_for=lambda _d: bounds)
    assert off.session_entries == 0 and off.check("TSLA", "macd_momentum") is None


def test_book_replay_caps_new_entries_per_session_and_feeds_the_rule():
    dates = pd.bdate_range("2026-01-05", periods=30).strftime("%Y-%m-%d")
    frames = {s: pd.DataFrame({"date": dates, "close": 100.0}) for s in ("AAPL", "MSFT", "JPM", "XOM", "PG")}

    def trade(symbol, entry, exit_, rank, strategy="macd_momentum"):
        return {"symbol": symbol, "strategy": strategy, "entry_idx": entry, "exit_idx": exit_,
                "entry_date": dates[entry], "exit_date": dates[exit_], "decision_date": dates[entry - 1],
                "barrier": "vertical", "confidence": 0.6, "live_rank": rank, "net_return": 0.01,
                "entry_price": 100.0, "exit_price": 101.0}

    rows = pd.DataFrame([trade("AAPL", 1, 3, 0), trade("MSFT", 1, 3, 1), trade("JPM", 1, 3, 2), trade("XOM", 1, 3, 3),
                         trade("PG", 5, 8, 4), trade("AAPL", 5, 8, 0)])
    assert len(book_replay.allocate(rows, frames, {})) == 6
    capped = book_replay.allocate(rows, frames, {"max_new_per_day": 2})
    assert sorted(zip(capped["symbol"], capped["entry_idx"])) == [("AAPL", 1), ("AAPL", 5), ("MSFT", 1), ("PG", 5)]
    assert len(book_replay.allocate(rows, frames, {"max_new_per_day": 4})) == 6
    rows_out = book_replay.book_rows(capped, frames, weight=0.05)
    assert set(rows_out) == set(engine.WINDOWS)
    assert {"trades", "expectancy_bps", "total_profit_bps", "portfolio_max_drawdown"} <= set(rows_out["all"])
    assert rows_out["all"]["trades"] == 4
    assert book_replay.book_rows(rows.iloc[:0], frames, weight=0.05)["all"]["trades"] == 0
    policy = book_replay.book_policy({"max_new_entries_per_session": 2, "stop_cooldown_sessions": 5})
    assert policy == {"caps": book_replay.BASE_CAPS, "order": "live", "stop_cooldown": 5, "max_new_per_day": 2}
    assert book_replay.book_policy({})["max_new_per_day"] == 0


def test_slot_allocation_script_still_runs_on_the_service():
    import sys
    from pathlib import Path

    scripts = Path(__file__).resolve().parents[1] / "scripts"
    sys.path.insert(0, str(scripts))
    try:
        import run_slot_allocation_backtest as sb
    finally:
        sys.path.remove(str(scripts))
    assert sb.allocate is not None and sb.BASE_CAPS == book_replay.BASE_CAPS and "max_new_2" in sb.POLICIES
    frames = {"SPY": _walk(rows=400, seed=11), "QQQ": _walk(rows=400, seed=12)}
    from app.services import meta_label_primary as primary

    replays = {s: primary.replay_signals(f) for s, f in frames.items()}
    rows = sb.candidates(frames, frames["SPY"], replays)
    assert {"symbol", "strategy", "confidence", "live_rank", "entry_date", "exit_date"} <= set(rows.columns)
    assert len(sb.allocate(rows, frames, sb.POLICIES["max_new_2"])) <= len(rows)


# --------------------------------------------------------------- templates

def test_every_diagnosed_cause_and_flag_now_has_a_template():
    assert [t.name for t in engine.templates_for({"missed_sector_move"})] == ["sector_gate"]
    assert [t.name for t in engine.templates_for({"weak_breadth_entry"})] == ["breadth_gate"]
    assert [t.name for t in engine.templates_for({"clustered_stops"})] == ["entry_cluster_cap"]
    assert [t.name for t in engine.templates_for({"costly_sector_gate"})] == ["sector_gate"]
    assert [t.name for t in engine.templates_for({"costly_breadth_gate"})] == ["breadth_gate"]
    by_name = engine.TEMPLATE_BY_NAME
    assert [by_name["sector_gate"].label(c) for c in by_name["sector_gate"].candidates] == ["off", "sector10", "sector20", "sector50"]
    assert [by_name["breadth_gate"].label(c) for c in by_name["breadth_gate"].candidates] == ["off", "breadth40", "breadth50", "breadth60"]
    assert [by_name["entry_cluster_cap"].label(c) for c in by_name["entry_cluster_cap"].candidates] == ["off", "cap2", "cap4"]
    assert by_name["sector_gate"].governance_action == "sector_gate_update"
    assert by_name["breadth_gate"].governance_action == "breadth_gate_update"
    assert by_name["entry_cluster_cap"].governance_action == "entry_cap_update"
    assert {t.name for t in engine.GATE_TEMPLATES} >= {"sector_gate", "breadth_gate"}
    assert [t.name for t in engine.BOOK_TEMPLATES] == ["entry_cluster_cap"]
    assert "50" in by_name["sector_gate"].describe({SECTOR_GATE_KEY: 50})
    assert "60%" in by_name["breadth_gate"].describe({"breadth_gate_min": 60.0})
    assert "4 new entries" in by_name["entry_cluster_cap"].describe({"max_new_entries_per_session": 4})
    # The three templates sit at the end of the block, after the exit templates.
    assert [t.name for t in engine.TEMPLATES[-3:]] == ["sector_gate", "breadth_gate", "entry_cluster_cap"]
    # The diagnosis proposals name the template that now tests them.
    assert "sector_gate" in PROPOSALS["missed_sector_move"]
    assert "breadth_gate" in CONTRIBUTING_PROPOSALS["weak_breadth_entry"]
    assert "entry_cluster_cap" in CONTRIBUTING_PROPOSALS["clustered_stops"]


def test_plan_targets_the_strategy_for_gates_and_the_book_for_the_cap():
    strategies = {"channel_breakout": object(), "rsi2_pullback": object()}
    lessons = [_lesson("channel_breakout", "missed_sector_move", 5, {"weak_breadth_entry": 4, "clustered_stops": 3}),
               _lesson("rsi2_pullback", "signal_wrong", 4, {"clustered_stops": 4})]
    plan = engine.plan_experiments(lessons, strategies, now=SUNDAY, history=[], cooldown_days=28, max_experiments=10)
    planned = {(p["strategy_type"], p["template"]): (p["trigger"], p["lesson_count"]) for p in plan["planned"]}
    assert planned[("channel_breakout", "sector_gate")] == ("missed_sector_move", 5)
    assert planned[("channel_breakout", "breadth_gate")] == ("weak_breadth_entry", 5)
    # One book experiment, whichever strategies the flag came from; the larger lesson wins the pair.
    assert planned[(engine.BOOK, "entry_cluster_cap")] == ("clustered_stops", 5)
    assert sum(p["template"] == "entry_cluster_cap" for p in plan["planned"]) == 1
    assert plan["untemplated"] == []
    # The cooldown applies to the book pair like any other.
    history = [{"strategy_type": engine.BOOK, "template": "entry_cluster_cap", "lesson_count": 5,
                "ran_at": (SUNDAY - timedelta(days=3)).isoformat()}]
    plan = engine.plan_experiments(lessons, strategies, now=SUNDAY, history=history, cooldown_days=28, max_experiments=10)
    assert all(p["template"] != "entry_cluster_cap" for p in plan["planned"])


# ------------------------------------------------------------- engine runs

@pytest.fixture()
def universe(monkeypatch):
    monkeypatch.setattr(settings, "paper_execution_symbols", ["SPY", "AAPL", "XLK"])
    monkeypatch.setattr(settings, "experiment_engine_max_experiments", 4)
    frames = {"SPY": _walk(rows=400, seed=3, start="2024-01-02"), "AAPL": _walk(rows=400, seed=4, start="2024-01-02"),
              "XLK": _walk(rows=400, seed=5, start="2024-01-02")}
    return {"loader": lambda symbol: frames[symbol], "earnings_loader": lambda symbol: pd.DataFrame(),
            "vix_loader": lambda start: pd.DataFrame(columns=["date", "close"])}


def _fake_strategy_replay(monkeypatch, key, scores, baseline_value=0):
    """``scores[label] = (expectancy, drawdown, trades)`` per value of ``key``; ``baseline_value`` is the live one."""
    template = next(t for t in engine.TEMPLATES if t.key == key)

    def simulate(strategy, frames, spy, setup, replays, vix=None, **extra):
        return pd.DataFrame({"value": [setup.get(key) or 0]})

    def window_rows(trades, frames):
        value = trades["value"].iloc[0]
        label = template.label({key: value})
        if value == baseline_value:
            label = "baseline"
        return {w: _window(*scores.get(label, scores["baseline"])) for w in engine.WINDOWS}

    monkeypatch.setattr(gate_replay, "window_rows", window_rows)
    return simulate


def _repeat(db, strategy_type, cause, count=4, contributing=()):
    for i in range(count):
        _attribution(db, strategy_type=strategy_type, cause=cause, net_return=-0.02,
                     closed_at=SUNDAY - timedelta(days=10 - i), contributing=contributing)
    _attribution(db, strategy_type=strategy_type, cause="edge", net_return=0.03, closed_at=SUNDAY - timedelta(days=4))


def test_run_adopts_the_sector_gate_on_the_strategy_row_and_the_seed_respects_it(db, monkeypatch, universe):  # noqa: F811
    row = _strategy(db, "channel_breakout")
    _repeat(db, "channel_breakout", "missed_sector_move")
    simulate = _fake_strategy_replay(monkeypatch, SECTOR_GATE_KEY, {
        "baseline": (5.0, 0.20, 200), "sector10": (6.0, 0.19, 190), "sector20": (12.0, 0.15, 150),
        "sector50": (14.0, 0.25, 120)})
    result = engine.run_experiment_engine(db, now=SUNDAY, simulate=simulate, **universe)
    assert result["status"] == "complete" and result["errors"] == []
    assert [e["template"] for e in result["experiments"]] == ["sector_gate"]
    assert result["experiments"][0]["adopted"] == "sector20" and result["experiments"][0]["applied"]
    db.refresh(row)
    assert row.parameters[SECTOR_GATE_KEY] == 20
    governance = db.scalars(select(AuditLog).where(AuditLog.action == "sector_gate_update")).all()
    assert len(governance) == 1 and governance[0].payload["source"] == "experiment_engine"
    change = engine.adopted_changes(db)[0]
    assert change["before"] == {SECTOR_GATE_KEY: None} and change["after"] == {SECTOR_GATE_KEY: 20}
    assert change["trigger"] == "missed_sector_move" and change["strategy_type"] == "channel_breakout"
    # Seed-style override contract: a later report's value never overrides the engine's decision.
    monkeypatch.setattr(seed, "SECTOR_GATED_STRATEGIES", {"channel_breakout": 50})
    assert seed.apply_sector_gates(db) == []
    row.parameters = {}
    db.flush()
    assert seed.apply_sector_gates(db) == []          # the governance row alone is enough
    other = _strategy(db, "trend_pullback")
    monkeypatch.setattr(seed, "SECTOR_GATED_STRATEGIES", {"channel_breakout": 50, "trend_pullback": 10})
    assert seed.apply_sector_gates(db) == ["trend_pullback"] and other.parameters[SECTOR_GATE_KEY] == 10
    assert seed.apply_sector_gates(db) == []
    # A revert restores the row through the same governance action.
    row.parameters = {SECTOR_GATE_KEY: 20}
    db.flush()
    assert engine.revert_change(db, change, now=SUNDAY, reason="test")
    db.refresh(row)
    assert SECTOR_GATE_KEY not in row.parameters


def test_run_replays_the_breadth_gate_from_the_history_and_fails_open_without_it(db, monkeypatch, universe):  # noqa: F811
    row = _strategy(db, "channel_breakout")
    _repeat(db, "channel_breakout", "signal_wrong", contributing=["weak_breadth_entry"])
    simulate = _fake_strategy_replay(monkeypatch, "breadth_gate_min", {
        "baseline": (5.0, 0.20, 200), "breadth40": (9.0, 0.19, 190), "breadth50": (12.0, 0.15, 150),
        "breadth60": (14.0, 0.25, 120)})
    loads = []

    def breadth_loader(now):
        loads.append(now)
        return pd.Series([55.0, 30.0], index=pd.to_datetime(["2024-01-02", "2024-06-03"]))

    result = engine.run_experiment_engine(db, now=SUNDAY, simulate=simulate, breadth_loader=breadth_loader, **universe)
    assert result["errors"] == [] and loads == [SUNDAY]
    assert result["experiments"][0]["adopted"] == "breadth50"
    db.refresh(row)
    assert row.parameters["breadth_gate_min"] == 50.0
    payload = db.scalar(select(AuditLog).where(AuditLog.action == engine.ACTION_EXPERIMENT)).payload
    assert payload["breadth_history"] is True
    assert db.scalars(select(AuditLog).where(AuditLog.action == "breadth_gate_update")).all()[0].payload["breadth_gate_min"] == 50.0
    monkeypatch.setattr(seed, "BREADTH_GATED_STRATEGIES", {"channel_breakout": 60.0})
    assert seed.apply_breadth_gates(db) == []
    # No breadth history: the experiment still runs (the gate fails open in replay as live) and says so.
    row2 = _strategy(db, "rsi2_pullback")
    _repeat(db, "rsi2_pullback", "signal_wrong", contributing=["weak_breadth_entry"])

    def broken(now):
        raise RuntimeError("no membership file")

    result = engine.run_experiment_engine(db, now=SUNDAY + timedelta(days=1), simulate=simulate, breadth_loader=broken,
                                          **universe)
    experiment = next(e for e in result["experiments"] if e["strategy_type"] == "rsi2_pullback")
    assert experiment["template"] == "breadth_gate" and result["errors"] == []
    rows = db.scalars(select(AuditLog).where(AuditLog.action == engine.ACTION_EXPERIMENT).order_by(AuditLog.id)).all()
    assert rows[-1].payload["breadth_history"] is False
    db.refresh(row2)


def _book_rows_by_count(monkeypatch, scores):
    """``scores[n] = (expectancy, drawdown, trades)`` for a book that accepted ``n`` candidate trades."""
    def book_rows(accepted, frames, *, weight):
        return {w: _window(*scores[len(accepted)]) for w in engine.WINDOWS}

    monkeypatch.setattr(book_replay, "book_rows", book_rows)


def _six_same_day_candidates(frames, spy, replays, setups, *, simulate, **extra):
    dates = frames["SPY"]["date"]
    symbols = ["AAPL", "MSFT", "JPM", "XOM", "PG", "UNH"]
    return pd.DataFrame([{"symbol": s, "strategy": "channel_breakout", "entry_idx": 10, "exit_idx": 14,
                          "entry_date": dates[10], "exit_date": dates[14], "decision_date": dates[9],
                          "barrier": "vertical", "confidence": 0.6, "live_rank": i, "net_return": 0.01,
                          "entry_price": 100.0, "exit_price": 101.0} for i, s in enumerate(symbols)])


def test_run_adopts_the_entry_cap_on_the_risk_rule_and_judges_the_book(db, monkeypatch, universe):  # noqa: F811
    monkeypatch.setattr(settings, "experiment_verdict_min_trades", 4)
    _strategy(db, "channel_breakout")
    _strategy(db, "rsi2_pullback")
    _repeat(db, "channel_breakout", "signal_wrong", contributing=["clustered_stops"])
    # Live caps accept 5 of the 6 same-day trades (<= 5 per strategy); cap2 accepts 2, cap4 accepts 4.
    _book_rows_by_count(monkeypatch, {5: (5.0, 0.20, 200), 4: (12.0, 0.15, 150), 2: (20.0, 0.10, 40)})
    simulate = _fake_strategy_replay(monkeypatch, "market_gate_sma", {"baseline": (1.0, 0.1, 100)})
    result = engine.run_experiment_engine(db, now=SUNDAY, simulate=simulate, book_candidates=_six_same_day_candidates,
                                          **universe)
    assert result["errors"] == []
    experiment = next(e for e in result["experiments"] if e["template"] == "entry_cluster_cap")
    assert experiment["strategy_type"] == engine.BOOK and experiment["adopted"] == "cap4" and experiment["applied"]
    rule = get_active_risk_rule(db)
    assert rule.value["max_new_entries_per_session"] == 4
    assert protection_config(rule.value).max_new_entries_per_session == 4   # the live protection reads it
    governance = db.scalars(select(AuditLog).where(AuditLog.action == "entry_cap_update")).all()
    assert len(governance) == 1 and governance[0].entity_type == "risk_rule"
    payload = db.scalar(select(AuditLog).where(AuditLog.action == engine.ACTION_EXPERIMENT,
                                               AuditLog.entity_type == "risk_rule")).payload
    assert set(payload["setups"]) == {"baseline", "cap2", "cap4"} and payload["book"]["candidate_trades"] == 6
    assert payload["decision"]["rule"] == engine.RULE and "total profit" in payload["decision"]["failed"]["cap2"]
    change = next(c for c in engine.adopted_changes(db) if c["template"] == "entry_cluster_cap")
    assert change["strategy_type"] == engine.BOOK and change["strategy_id"] is None
    assert change["before"] == {"max_new_entries_per_session": None} and change["after"] == {"max_new_entries_per_session": 4}
    assert change["recent_before"]["trades"] == 5
    # Seed-style override contract on the risk rule.
    monkeypatch.setattr(seed, "ENTRY_CAP_PER_SESSION", 2)
    assert seed.apply_entry_cap(db) is False and get_active_risk_rule(db).value["max_new_entries_per_session"] == 4
    # The verdict counts every strategy's trades after the change; a bad book reverts the cap to off.
    for i in range(4):
        _attribution(db, strategy_type="rsi2_pullback" if i % 2 else "channel_breakout", cause="signal_wrong",
                     net_return=-0.05, closed_at=SUNDAY + timedelta(days=2 + i), opened_at=SUNDAY + timedelta(days=1 + i),
                     contributing=["clustered_stops"])
    review = engine.review_changes(db, now=SUNDAY + timedelta(days=8))
    verdict = next(v for v in review["reviewed"] if v["template"] == "entry_cluster_cap")
    assert verdict["after"]["trades"] == 4 and verdict["verdict"] == "not_helping" and verdict["reverted"]
    assert verdict["shadow"] is None
    assert get_active_risk_rule(db).value["max_new_entries_per_session"] == 0
    assert db.scalars(select(AuditLog).where(AuditLog.action == "entry_cap_update")).all()[-1].payload["reverted_change"] == change["change_id"]
    assert seed.apply_entry_cap(db) is False


def test_real_replays_run_end_to_end_for_the_three_templates(db, monkeypatch, universe):  # noqa: F811
    monkeypatch.setattr(settings, "experiment_verdict_min_trades", 4)
    _strategy(db, "channel_breakout")
    _repeat(db, "channel_breakout", "missed_sector_move", contributing=["weak_breadth_entry", "clustered_stops"])
    breadth = pd.Series(np.linspace(30, 70, 400), index=pd.bdate_range("2024-01-02", periods=400))
    result = engine.run_experiment_engine(db, now=SUNDAY, breadth_loader=lambda now: breadth, **universe)
    assert result["status"] == "complete" and result["errors"] == []
    assert {e["template"] for e in result["experiments"]} == {"sector_gate", "breadth_gate", "entry_cluster_cap"}
    for row in db.scalars(select(AuditLog).where(AuditLog.action == engine.ACTION_EXPERIMENT)).all():
        payload = row.payload
        assert set(payload["rows"]["baseline"]) == set(engine.WINDOWS)
        assert {"trades", "expectancy_bps", "total_profit_bps", "portfolio_max_drawdown"} <= set(payload["rows"]["baseline"]["all"])
    ledger = engine.experiment_ledger(db)
    assert {t["name"] for t in ledger["templates"]} >= {"sector_gate", "breadth_gate", "entry_cluster_cap"}
    assert next(t for t in ledger["templates"] if t["name"] == "entry_cluster_cap")["kind"] == "book"


# ------------------------------------------------------------------- cycle

_MACD = SimpleNamespace(id=3, strategy_type="macd_momentum", parameters={SECTOR_GATE_KEY: 20})


def _cycle(strategies, protections, *, symbols):
    order = SimpleNamespace(id=42, status="accepted")
    db = _ModelDb(strategies, {})
    with ExitStack() as stack:
        stack.enter_context(patch.object(settings, "paper_execution_symbols", symbols))
        stack.enter_context(patch("app.services.stock_paper_autotrader.active_paper_account", return_value=_account()))
        stack.enter_context(patch("app.services.stock_paper_autotrader._is_regular_session", return_value=True))
        stack.enter_context(patch("app.services.stock_paper_autotrader.cancel_stale_exit_entries", return_value=[]))
        stack.enter_context(signal_source(False, return_value={
            "signal_id": 7, "signal_action": "BUY", "reference_price": "100"}))
        stack.enter_context(patch("app.services.stock_paper_autotrader._strategy_holdings", return_value=({}, True)))
        stack.enter_context(patch("app.services.stock_paper_autotrader._pending_orders", return_value=[]))
        stack.enter_context(patch("app.services.stock_paper_autotrader.broker_stop_coverage", return_value=Decimal("0")))
        loader = stack.enter_context(patch("app.services.stock_paper_autotrader.load_entry_protections",
                                           return_value=protections))
        reserve = stack.enter_context(patch("app.services.stock_paper_autotrader.reserve_stock_paper_order",
                                            return_value=order))
        stack.enter_context(patch("app.services.stock_paper_autotrader.dispatch_reserved_order", return_value=order))
        result = run_stock_paper_signal_cycle(db)
    return result, reserve, loader


@pytest.mark.usefixtures("legacy_technical_exits")
def test_cycle_withholds_a_sector_gated_buy_and_wires_the_gates():
    protections = EntryProtections(now=NOW, config=ProtectionConfig(), sector_gates={"macd_momentum": 20},
                                   sector_history={"XLK": FALLING})
    result, reserve, loader = _cycle([_MACD], protections, symbols=["AAPL"])
    reserve.assert_not_called()
    row = result["results"][0]
    assert row["status"] == "skipped" and row["protection"] == "sector_gate"
    assert "entry protection sector_gate" in row["reason"] and "XLK" in row["reason"]
    assert result["status"] == "complete" and "sector_gate" in result["entry_protections"]
    # The cycle hands the loader each row's sector / breadth gate, its symbols and the gauges.
    kwargs = loader.call_args.kwargs
    assert kwargs["sector_gates"] == {"macd_momentum": 20} and kwargs["breadth_gates"] == {"macd_momentum": 0.0}
    assert list(kwargs["symbols"]) == ["AAPL"] and "gauges" in kwargs
    # A rising sector lets the same BUY through to the reservation.
    allowed = EntryProtections(now=NOW, config=ProtectionConfig(), sector_gates={"macd_momentum": 20},
                               sector_history={"XLK": RISING})
    result, reserve, _ = _cycle([_MACD], allowed, symbols=["AAPL"])
    assert reserve.call_count == 1 and result["results"][0]["status"] == "submitted"


@pytest.mark.usefixtures("legacy_technical_exits")
def test_cycle_counts_its_own_buys_against_the_session_entry_cap():
    protections = EntryProtections(now=NOW, config=ProtectionConfig(max_new_entries_per_session=1))
    result, reserve, _ = _cycle([_MACD], protections, symbols=["AAPL", "MSFT"])
    assert reserve.call_count == 1
    statuses = {r["symbol"]: r for r in result["results"]}
    assert statuses["AAPL"]["status"] == "submitted"
    assert statuses["MSFT"]["status"] == "skipped" and statuses["MSFT"]["protection"] == "entry_cap"
    assert protections.cycle_entries == 1
