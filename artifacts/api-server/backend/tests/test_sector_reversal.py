"""Sector-relative reversal: features, strategy, replay replica, ranking, backtest decision rules, seed contract."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select

from app.models import AuditLog, Strategy
from app.services import gate_replay, sector_ranking, sector_relative, seed
from app.services import meta_label_primary as primary
from app.services import strategy_discovery as sd
from app.services.strategies import rules, sector_reversal
from app.services.strategies.registry import get_strategy, is_known_strategy
from app.services.strategies.sector_reversal import DEFAULT_RULE, SectorReversalStrategy
from app.services.strategies.short_term_reversion import wilder_rsi

from tests.test_loss_feedback_loop import db  # noqa: F401  (fixture)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_sector_reversal_backtest as bt  # noqa: E402

REL_FEATURES = ("rel_ret_1", "rel_ret_5", "rel_rsi_2", "rel_z_5")


@pytest.fixture(autouse=True)
def _clear_runtime():
    sector_relative.clear_cache()
    yield
    sector_relative.clear_cache()


def _pair(rows=620, seed=3, drops=(300, 380, 460, 540), start="2015-01-02"):
    """A trending stock and its sector ETF, with sharp stock-only 5-day falls planted at ``drops``."""
    rng = np.random.default_rng(seed)
    sector_steps = rng.normal(0.0004, 0.010, rows)
    own = rng.normal(0.0012, 0.008, rows)
    for bar in drops:
        own[bar - 4:bar + 1] = -0.025
        own[bar + 1:bar + 4] = 0.02
    sector = 100 * np.exp(np.cumsum(sector_steps))
    close = 50 * np.exp(np.cumsum(sector_steps + own))
    open_ = close * (1 + rng.normal(0, 0.003, rows))
    high = np.maximum(open_, close) * 1.004
    low = np.minimum(open_, close) * 0.996
    dates = pd.bdate_range(start, periods=rows).strftime("%Y-%m-%d")
    stock = pd.DataFrame({"date": dates, "open": open_, "high": high, "low": low, "close": close,
                          "volume": 2_000_000.0, "sector_close": sector})
    etf = pd.DataFrame({"date": dates, "open": sector, "high": sector * 1.003, "low": sector * 0.997,
                        "close": sector, "volume": 5_000_000.0})
    return stock, etf


# ---------------------------------------------------------------- features

def test_relative_features_match_their_definitions_and_ignore_the_sector_scale():
    stock, _ = _pair()
    close, sector = stock["close"], stock["sector_close"]
    np.testing.assert_allclose(rules.feature(stock, "rel_ret_1"), close.pct_change() - sector.pct_change(), equal_nan=True)
    np.testing.assert_allclose(rules.feature(stock, "rel_ret_5"),
                               close / close.shift(5) - sector / sector.shift(5), equal_nan=True)
    np.testing.assert_allclose(rules.feature(stock, "rel_rsi_2"), wilder_rsi(close / sector, 2), equal_nan=True)
    rel5 = close / close.shift(5) - sector / sector.shift(5)
    z = (rel5 - rel5.rolling(60).mean()) / rel5.rolling(60).std()
    np.testing.assert_allclose(rules.feature(stock, "rel_z_5"), z, equal_nan=True)
    scaled = stock.assign(sector_close=stock["sector_close"] * 7.3)
    for name in REL_FEATURES:
        np.testing.assert_allclose(rules.feature(stock, name), rules.feature(scaled, name), rtol=1e-9, equal_nan=True)
    # Without the column (an ETF, an unmapped name) every relative feature is NaN and a rule never fires.
    bare = stock.drop(columns=["sector_close"])
    assert all(rules.feature(bare, name).isna().all() for name in REL_FEATURES)
    assert not rules.entry_signals(bare, DEFAULT_RULE).any()
    assert rules.rule_lookback(DEFAULT_RULE) == 201 and rules._lookback("rel_z_5") == 66
    assert rules.uses_sector(DEFAULT_RULE) and not rules.uses_sector({"entry": [{"f": "rsi_2", "op": "<", "v": 5}]})


def test_relative_features_are_causal_on_every_prefix():
    stock, _ = _pair(rows=420)
    noisy = stock.copy()
    noisy.loc[301:, ["open", "high", "low", "close"]] *= 1.6
    noisy.loc[301:, "sector_close"] *= 0.7
    for name in REL_FEATURES:
        full = rules.feature(stock, name).to_numpy()
        np.testing.assert_allclose(full[:301], rules.feature(noisy, name).to_numpy()[:301], equal_nan=True, err_msg=name)
        for t in range(70, 420, 7):
            np.testing.assert_allclose(rules.feature(stock.iloc[:t + 1], name).to_numpy(), full[:t + 1],
                                       equal_nan=True, err_msg=f"{name} at {t}")


# ---------------------------------------------------------------- strategy

def test_strategy_buys_a_stock_only_fall_in_an_uptrend_and_exits_with_hysteresis():
    stock, _ = _pair()
    strategy = get_strategy("sector_reversal")
    assert is_known_strategy("sector_reversal") and isinstance(SectorReversalStrategy(), SectorReversalStrategy)
    policy = SectorReversalStrategy().exit_policy()
    assert (policy.stop_loss_pct, policy.take_profit_pct, policy.max_holding_sessions, policy.indicator_exit) == (
        0.10, 0.0, 10, True)
    signal = strategy.generate_signal("AAPL", stock.iloc[:301])
    assert signal.action == "BUY" and signal.confidence >= gate_replay.MIN_CONFIDENCE
    assert signal.features["rel_z_5"] < -2 and signal.features["dist_sma_200"] > 0
    # The same fall shared by the sector is not a relative extreme.
    shared = stock.assign(sector_close=stock["close"] * 0.5)
    assert strategy.generate_signal("AAPL", shared.iloc[:301]).action == "HOLD"
    # Below the 200-day average: no entry.
    falling = stock.copy()
    falling.loc[:, ["open", "high", "low", "close"]] = falling[["open", "high", "low", "close"]].to_numpy() \
        * np.linspace(3.0, 1.0, len(falling))[:, None]
    assert rules.feature(falling.iloc[:301], "dist_sma_200").iloc[-1] < 0
    assert strategy.generate_signal("AAPL", falling.iloc[:301]).action == "HOLD"
    # Exit: not on the entry bar (still stretched), yes once the relative move reverted.
    assert strategy.exit_signal("AAPL", stock.iloc[:301], 0) is None
    exits = rules.exit_signals(stock, DEFAULT_RULE)
    first = 301 + int(np.flatnonzero(exits[301:])[0])
    assert first <= 305 and strategy.exit_signal("AAPL", stock.iloc[:first + 1], 0) == "rule_exit"
    # ETFs and unmapped names have no benchmark.
    assert strategy.generate_signal("XLK", stock.drop(columns=["sector_close"])).action == "HOLD"
    assert strategy.generate_signal("ZZZZ", stock.drop(columns=["sector_close"])).action == "HOLD"


def test_runtime_attaches_the_sector_etf_and_fails_closed_without_it():
    stock, etf = _pair()
    bare = stock.drop(columns=["sector_close"]).iloc[:301]
    closes = pd.Series(etf["close"].to_numpy(), index=pd.to_datetime(etf["date"]))
    sector_relative.set_runtime_sector("XLK", closes * 3.1)   # AAPL -> XLK in the static map
    strategy = get_strategy("sector_reversal")
    assert strategy.generate_signal("AAPL", bare).action == "BUY"
    assert sector_ranking.rank_value("AAPL", bare) == pytest.approx(rules.feature(stock.iloc[:301], "rel_z_5").iloc[-1])
    sector_relative.set_runtime_sector("XLK", None)
    signal = strategy.generate_signal("AAPL", bare)
    assert signal.action == "HOLD" and "sector" in signal.reason.lower()
    assert sector_ranking.rank_value("AAPL", bare) is None


# ---------------------------------------------------------------- replica and replay

def test_replica_matches_the_class_on_every_prefix_and_replays_through_gate_replay():
    stock, etf = _pair()
    replay = primary.sector_reversal(stock)
    exits = primary.replay_exit_signals(stock, "sector_reversal")
    strategy = SectorReversalStrategy()
    assert (replay["action"] == "BUY").sum() >= 3
    for t in range(201, len(stock)):
        prefix = stock.iloc[:t + 1]
        assert (strategy.generate_signal("AAPL", prefix).action == "BUY") == (replay["action"].iloc[t] == "BUY"), t
        assert bool(strategy.exit_signal("AAPL", prefix, 0)) == bool(exits[t]), t
    assert (replay["confidence"][replay["action"] == "BUY"] == 0.6).all()
    assert (primary.sector_reversal(stock.drop(columns=["sector_close"]))["action"] == "HOLD").all()
    assert "sector_reversal" not in primary.REPLAYABLE_STRATEGIES  # the meta-label feature set is unchanged
    assert gate_replay.replayable("sector_reversal")
    assert gate_replay.barrier_for("sector_reversal").stop_loss_pct == 0.10
    assert gate_replay.scope("sector_reversal", ["AAPL", "SPY", "XLK"]) == ["AAPL"]
    frames = {"AAPL": stock, "XLK": etf}
    trades = gate_replay.simulate("sector_reversal", frames, None, {}, None, calibration=None)
    assert len(trades) >= 3 and set(trades["strategy"]) == {"sector_reversal"}
    assert (trades["holding_days"] <= 10).all()


# ---------------------------------------------------------------- sector map

def test_static_map_falls_back_before_the_younger_spdrs_and_chains_returns():
    dates = pd.bdate_range("2018-01-02", periods=200)
    xlk = pd.Series(np.linspace(50, 60, 200), index=dates)
    xlc = pd.Series(np.linspace(40, 45, 120), index=dates[80:])
    assignment = sector_relative.static_assignment("META", dates, {"XLK": xlk, "XLC": xlc})
    assert set(assignment[:80]) == {"XLK"} and set(assignment[80:]) == {"XLC"}
    index = sector_relative.chained_index(dates, assignment, {"XLK": xlk, "XLC": xlc})
    np.testing.assert_allclose(index[1:80] / index[:79], (xlk / xlk.shift(1)).to_numpy()[1:80])
    np.testing.assert_allclose(index[81:] / index[80:-1], (xlc / xlc.shift(1)).to_numpy()[1:])
    assert sector_relative.static_sector("SPY") is None and sector_relative.static_sector("XLK") is None
    assert sector_relative.static_sector("AAPL") == "XLK" and sector_relative.static_sector("ZZZZ") is None


def test_statistical_assignment_is_causal_and_monthly():
    rng = np.random.default_rng(1)
    dates = pd.bdate_range("2010-01-04", periods=700)
    market = rng.normal(0.0003, 0.01, 700)
    factors = {etf: rng.normal(0, 0.008, 700) for etf in ("XLE", "XLK", "XLV")}
    closes = {etf: pd.Series(100 * np.exp(np.cumsum(market + f)), index=dates) for etf, f in factors.items()}
    spy = pd.Series(100 * np.exp(np.cumsum(market)), index=dates)
    own = np.where(np.arange(700) < 400, factors["XLE"], factors["XLV"])  # changes industry at bar 400
    stock = pd.Series(30 * np.exp(np.cumsum(market + own + rng.normal(0, 0.004, 700))), index=dates)
    assigned = sector_relative.assign_sectors(stock, closes, spy)
    assert assigned.iloc[:126].isna().all()                       # not enough history yet
    assert (assigned.iloc[200:400] == "XLE").all()
    assert (assigned.iloc[-60:] == "XLV").all()
    months = pd.Series(assigned.to_numpy(), index=dates.to_period("M"))
    assert months.groupby(level=0).nunique(dropna=False).max() == 1  # constant inside a month
    # Causal: changing the future never changes an earlier month's assignment.
    later = stock.copy()
    later.iloc[500:] *= np.exp(np.cumsum(rng.normal(0, 0.05, 200)))
    np.testing.assert_array_equal(sector_relative.assign_sectors(later, closes, spy).iloc[:480].to_numpy(),
                                  assigned.iloc[:480].to_numpy())


def test_load_frames_attaches_the_sector_close_to_mapped_single_stocks():
    stock, etf = _pair(rows=400)
    raw = {"AAPL": stock.drop(columns=["sector_close"]), "XLK": etf, "SPY": etf}
    frames, missing = sd.load_frames(raw, loader=lambda s: raw[s], start="2000-01-01", end="2030-01-01",
                                     earnings_loader=lambda s: None, news_loader=lambda s: None)
    assert not missing and "sector_close" not in frames["XLK"]
    np.testing.assert_allclose(rules.feature(frames["AAPL"], "rel_z_5"), rules.feature(stock, "rel_z_5"),
                               rtol=1e-8, equal_nan=True)


def test_library_carries_the_sector_variants_on_single_stocks_only():
    variants = sd.sector_reversal_variants()
    assert len(variants) == 8 and variants[0][1] == DEFAULT_RULE
    library = sd.candidate_library(["AAPL", "MSFT", "SPY", "XLK"])
    family = [row for row in library if row["family"] == "sector_reversal"]
    assert len(family) == 8 and {row["universe"] for row in family} == {"single_stocks"}
    assert len({row["id"] for row in family}) == 8


# ---------------------------------------------------------------- ranking

def test_ranks_put_the_most_negative_relative_move_first_and_unrankable_last():
    buys = [("AAA", "rsi2_pullback"), ("BBB", "rsi2_pullback"), ("SPY", "rsi2_pullback"), ("CCC", "rsi2_pullback"),
            ("AAA", "trend_pullback")]
    ranks = sector_ranking.assign_ranks(buys, {"AAA": -1.0, "BBB": -3.0, "SPY": None, "CCC": -1.0})
    order = sorted((v["rank"], s) for (s, t), v in ranks.items() if t == "rsi2_pullback")
    assert [s for _, s in order] == ["BBB", "AAA", "CCC", "SPY"]           # ties keep the cycle order
    assert ranks[("BBB", "rsi2_pullback")] == {"by": "rel_z_5", "value": -3.0, "rank": 1, "of": 4}
    assert ranks[("AAA", "trend_pullback")]["rank"] == 1
    outcomes = {(s, t): {"signal_action": "BUY", "sector_rank": r} for (s, t), r in ranks.items()}
    outcomes[("DDD", "macd_momentum")] = {"signal_action": "BUY"}
    symbols = ["SPY", "DDD", "AAA", "EEE", "BBB", "CCC"]
    assert sector_ranking.order_symbols(symbols, outcomes) == ["BBB", "DDD", "AAA", "EEE", "CCC", "SPY"]
    assert sector_ranking.order_symbols(symbols, {}) == symbols
    assert sector_ranking.ranked({"rank_by": "rel_z_5"}) and not sector_ranking.ranked({})


def test_the_signal_batch_records_rank_and_value_for_a_ranked_strategy(monkeypatch):
    from types import SimpleNamespace

    from app.services import stock_paper_signal_batch as batch
    from app.services.strategies.base import StrategySignalResult

    stock, etf = _pair()
    frames = {"AAA": stock.drop(columns=["sector_close"]).iloc[:301],
              "BBB": stock.drop(columns=["sector_close"]).iloc[:290]}
    closes = pd.Series(etf["close"].to_numpy(), index=pd.to_datetime(etf["date"]))
    monkeypatch.setattr(sector_relative, "static_sector", lambda symbol: "XLK")
    sector_relative.set_runtime_sector("XLK", closes)
    row = SimpleNamespace(strategy_type="rsi2_pullback", parameters={"rank_by": "rel_z_5"}, id=1)
    buy = StrategySignalResult(action="BUY", probability_up=0.6, probability_down=0.4, confidence=0.7, reason="x",
                               features={})
    persisted = []
    monkeypatch.setattr(batch.ledger, "_resolve_signal_strategy", lambda db, t, scope: row)
    monkeypatch.setattr(batch, "_prefetch_inputs", lambda db, symbols: {s: (frames[s], "test", {"close": 1}) for s in symbols})
    monkeypatch.setattr(batch, "_generate", lambda *args: StrategySignalResult(**{**buy.__dict__, "features": {}}))
    monkeypatch.setattr(batch, "symbol_scope_for", lambda row: None)
    monkeypatch.setattr(batch, "daily_bar_execution", lambda row: False)
    monkeypatch.setattr(batch, "_spy_loader", lambda db: (lambda: None))
    monkeypatch.setattr(batch.ledger, "_build_stock_paper_signal",
                        lambda row, symbol, generated, *rest: persisted.append((symbol, generated.features)) or
                        SimpleNamespace(id=len(persisted), symbol=symbol, action="BUY", confidence=0.7,
                                        features=generated.features))
    monkeypatch.setattr(batch, "_persist_signals", lambda db, rows: None)
    db = SimpleNamespace(commit=lambda: None, rollback=lambda: None)
    outcomes, _stats = batch.generate_cycle_signals(db, ["BBB", "AAA"], [row])
    ranks = {symbol: features["sector_rank"] for symbol, features in persisted}
    assert ranks["AAA"]["rank"] == 1 and ranks["BBB"]["rank"] == 2           # AAA's fall is deeper
    assert ranks["AAA"]["value"] < ranks["BBB"]["value"]
    assert outcomes[("AAA", "rsi2_pullback")]["sector_rank"] == ranks["AAA"]
    assert sector_ranking.order_symbols(["BBB", "AAA"], outcomes) == ["AAA", "BBB"]


# ---------------------------------------------------------------- backtest decision rules

def _gate(tier, failed=()):
    checks = {key: key not in failed for key in (*sd.EXPLORE_CHECKS, "deflated_sharpe")}
    return {"tier": tier, "checks": checks}


def test_admission_needs_every_explore_check_and_ignores_the_deflated_sharpe():
    assert bt.admission_decision(_gate("explore", ["deflated_sharpe"]))["admitted"] is True
    assert bt.admission_decision(_gate("proven"))["status"] == "paper_trading_candidate"
    rejected = bt.admission_decision(_gate("rejected", ["holdout_positive"]))
    assert rejected == {**rejected, "admitted": False, "status": "paused", "failed": ["holdout_positive"]}


def _rows(expectancy, drawdown):
    return {w: {"expectancy_bps": e, "portfolio_max_drawdown": d} for w, e, d in zip(bt.WINDOWS, expectancy, drawdown)}


def test_ranking_is_adopted_only_when_no_window_is_worse():
    live = _rows([10, 12, 8], [0.10, 0.08, 0.05])
    assert bt.ranking_decision(live, _rows([10, 15, 9], [0.10, 0.07, 0.05]), changed=True)["adopted"] is True
    worse = bt.ranking_decision(live, _rows([11, 15, 7.9], [0.09, 0.07, 0.05]), changed=True)
    assert worse["adopted"] is False and "2020+ expectancy" in worse["reason"]
    deeper = bt.ranking_decision(live, _rows([11, 15, 9], [0.11, 0.07, 0.05]), changed=True)
    assert deeper["adopted"] is False and "all drawdown" in deeper["reason"]
    same = bt.ranking_decision(live, live, changed=False)
    assert same["adopted"] is False and same["reason"] == "no_effect"
    assert bt.ranking_decision(live, _rows([None, 15, 9], [0.1, 0.07, 0.05]), changed=True)["adopted"] is False


def test_vix_state_is_high_above_the_trailing_median():
    dates = pd.bdate_range("2020-01-01", periods=300)
    vix = pd.DataFrame({"date": dates.strftime("%Y-%m-%d"), "close": np.r_[np.full(280, 15.0), np.full(20, 30.0)]})
    states = bt.vix_states(vix)
    assert states.iloc[:125].isna().all() and states.iloc[200] == "low" and states.iloc[-1] == "high"
    assert list(bt.state_of(["2020-12-31"], states)) == [states.loc[:"2020-12-31"].iloc[-1]]


def test_order_rows_ranks_by_rel_z_with_unrankable_last():
    rows = pd.DataFrame({"rel_z_5": [-1.0, np.nan, -3.0], "confidence": [0.9, 0.8, 0.7]})
    ordered, order = bt.order_rows(rows, "rel_z_5")
    assert order == "confidence" and list(ordered.sort_values("confidence", ascending=False).index) == [2, 0, 1]
    assert bt.order_rows(rows, "live") == (rows, "live")


# ---------------------------------------------------------------- seed contract

def test_seed_creates_the_row_paused_once_and_the_operator_choice_stands(db, monkeypatch):  # noqa: F811
    monkeypatch.setattr(sector_reversal, "ADMITTED", False)
    assert seed.admit_sector_reversal(db) == "paused"
    row = db.scalars(select(Strategy).where(Strategy.strategy_type == "sector_reversal")).one()
    assert row.parameters["rule"] == DEFAULT_RULE and row.parameters["max_holding_sessions"] == 10
    assert "AAPL" in row.parameters["symbol_scope"] and "SPY" not in row.parameters["symbol_scope"]
    event = db.scalars(select(AuditLog).where(AuditLog.action == seed.SECTOR_REVERSAL_ACTION)).one()
    assert event.payload["admitted"] is False and event.payload["paper_only"] is True
    row.current_status = "research"
    db.flush()
    assert seed.admit_sector_reversal(db) is None and row.current_status == "research"
    db.delete(row)
    db.flush()
    assert seed.admit_sector_reversal(db) is None  # deleted on purpose: never recreated
    assert db.query(Strategy).filter(Strategy.strategy_type == "sector_reversal").count() == 0


def test_seed_admits_a_candidate_when_the_backtest_did(db, monkeypatch):  # noqa: F811
    monkeypatch.setattr(sector_reversal, "ADMITTED", True)
    assert seed.admit_sector_reversal(db) == "paper_trading_candidate"
    row = db.scalars(select(Strategy).where(Strategy.strategy_type == "sector_reversal")).one()
    assert get_strategy(row.strategy_type, row.parameters).slug == "sector_reversal"
    assert seed.ensure_strategy_lineup(db)["sector_reversal_admission"] is None


def test_sector_ranking_is_applied_once_to_adopted_strategies(db, monkeypatch):  # noqa: F811
    db.add(Strategy(name="RSI(2) Pullback", strategy_type="rsi2_pullback", parameters={}, is_active=True,
                    current_status="paper_trading_candidate"))
    db.flush()
    monkeypatch.setattr(seed, "SECTOR_RANKED_STRATEGIES", {"rsi2_pullback": {"rank_by": "rel_z_5"}})
    assert seed.apply_sector_ranking(db) == ["rsi2_pullback"]
    row = db.scalars(select(Strategy).where(Strategy.strategy_type == "rsi2_pullback")).one()
    assert row.parameters["rank_by"] == "rel_z_5"
    row.parameters = {}
    db.flush()
    assert seed.apply_sector_ranking(db) == []  # the operator removed it: never re-applied
