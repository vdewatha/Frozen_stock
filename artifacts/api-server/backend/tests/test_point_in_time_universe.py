"""Point-in-time universe: membership parsing, boundaries, the replay mask, coverage, the setting plumbing."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from app.core.config import settings
from app.services import experiment_engine as engine
from app.services import gate_replay
from app.services import point_in_time_universe as pit
from app.services import strategy_discovery as sd
from tests.test_experiment_engine import _repeat_market_losses, _strategy, db  # noqa: F401  (fixture)

UTC = timezone.utc
NOW = datetime(2026, 11, 6, 15, tzinfo=UTC)
FIXTURE = """ticker,start_date,end_date
AAPL,1996-01-02,
AAL,1996-01-02,1997-01-15
AAL,2015-03-23,2024-09-23
FB,2013-12-23,2022-06-09
META,2022-06-09,
AMD,1996-01-02,2013-09-23
AMD,2017-03-20,
BRK.B,2010-02-16,
OLD,1998-05-01,1999-12-31
BAD,,
"""


def _membership(now=NOW) -> pit.Membership:
    return pit.Membership.from_frame(pit.parse_membership(FIXTURE))


# --------------------------------------------------------------- parsing

def test_parse_merges_aliases_and_drops_bad_rows():
    frame = pit.parse_membership(FIXTURE)
    assert "FB" not in set(frame["ticker"]) and "BAD" not in set(frame["ticker"])
    meta = frame[frame["ticker"] == "META"]
    assert len(meta) == 1 and str(meta["start"].iloc[0].date()) == "2013-12-23" and pd.isna(meta["end"].iloc[0])
    aal = frame[frame["ticker"] == "AAL"]
    assert [str(d.date()) for d in aal["start"]] == ["1996-01-02", "2015-03-23"]
    assert pit.yahoo_symbol("BRK.B") == "BRK-B"


def test_members_on_at_change_boundaries():
    m = _membership()
    assert "AAL" in m.members_on("1997-01-14") and "AAL" not in m.members_on("1997-01-15")   # end is exclusive
    assert "AAL" not in m.members_on("2015-03-22") and "AAL" in m.members_on("2015-03-23")   # start is inclusive
    assert "AAL" not in m.members_on("2024-09-23")
    assert "META" in m.members_on("2014-01-02") and "META" in m.members_on("2026-11-05")
    assert "AMD" not in m.members_on("2015-06-01") and "AMD" in m.members_on("2017-03-20")
    assert "OLD" not in m.members_on("2000-01-03")
    assert "SPY" not in m.members_on("2020-01-02")  # ETFs are not index members
    assert m.tickers(since="2000-01-01") == ["AAL", "AAPL", "AMD", "BRK.B", "META"]
    assert "OLD" in m.tickers(since=None)


def test_mask_is_per_bar_with_etfs_always_in_and_unknown_never():
    m = _membership()
    dates = pd.DatetimeIndex(["1997-01-13", "1997-01-15", "2015-03-23", "2024-09-20", "2024-09-23"])
    assert m.mask("AAL", dates).tolist() == [True, False, True, True, False]
    assert m.mask("XLK", dates).all() and m.mask("SPY", dates).all()
    assert not m.mask("ZZZZ", dates).any()
    assert m("aapl", ["2020-01-02"]).tolist() == [True]  # callable, case-insensitive
    assert pit.membership_mask("AAL", dates, m).tolist() == m.mask("AAL", dates).tolist()
    assert pit.members_on("2020-01-02", m) == m.members_on("2020-01-02")


# ---------------------------------------------------------------- caching

def test_load_membership_caches_refreshes_monthly_and_keeps_the_old_file(tmp_path):
    path = tmp_path / "universe" / "sp500_membership.csv"
    good = FIXTURE + "".join(f"T{i},2001-01-02,\n" for i in range(pit.MIN_SOURCE_ROWS))
    calls = []

    def fetch():
        calls.append(1)
        return good

    m = pit.load_membership(download=True, now=NOW, fetch=fetch, path=path)
    assert "AAPL" in m.members_on("2020-01-02") and len(calls) == 1
    meta = json.loads(path.with_suffix(".json").read_text())
    assert meta["source"] == pit.SOURCE_URL and "MIT" in meta["licence"] and meta["downloaded_at"].startswith("2026-11-06")
    # fresh cache: no refetch
    pit.load_membership(download=True, now=NOW, fetch=fetch, path=path)
    assert len(calls) == 1
    # a bad refresh keeps the old file
    later = datetime(2026, 12, 20, tzinfo=UTC)
    m2 = pit.load_membership(download=True, now=later, fetch=lambda: "ticker,start_date,end_date\nX,2000-01-01,\n",
                             path=path)
    assert "AAPL" in m2.members_on("2020-01-02")
    m3 = pit.load_membership(download=True, now=later, fetch=lambda: (_ for _ in ()).throw(OSError("offline")), path=path)
    assert "AAPL" in m3.members_on("2020-01-02")
    with pytest.raises(FileNotFoundError):
        pit.load_membership(download=False, now=NOW, path=tmp_path / "none.csv")


def _bars(start: str, end: str) -> pd.DataFrame:
    dates = pd.bdate_range(start, end)
    return pd.DataFrame({"date": dates.strftime("%Y-%m-%d"), "open": 10.0, "high": 10.5, "low": 9.5, "close": 10.0,
                         "volume": 1000})


def test_coverage_with_a_fake_loader_never_fails_on_one_symbol(tmp_path):
    m = _membership()
    histories = {"AAPL": _bars("1990-01-02", "2026-11-05"), "AMD": _bars("2016-01-04", "2026-11-05"),
                 "META": _bars("2012-05-18", "2026-11-05"), "BRK.B": _bars("2026-10-01", "2026-11-05")}

    def loader(ticker):
        if ticker == "AAL":
            raise KeyError("date")  # what yfinance gives for a delisted ticker
        return histories[ticker]

    cache = tmp_path / "sp500_coverage.json"
    cov = pit.coverage(m, loader=loader, now=NOW, cache_path=cache)
    assert cov["symbols"]["AAL"]["status"] == "missing" and cov["symbols"]["BRK.B"]["status"] == "empty"
    assert cov["available"] == ["AAPL", "AMD", "META"] and cov["missing"] == ["AAL", "BRK.B"]
    # 2016: members AAL, AAPL, BRK.B, META (AMD rejoined 2017) -> AAPL and META have data
    assert cov["by_year"]["2016"] == {"members": 4, "with_data": 2, "coverage": 0.5}
    # 2005: members AAL? no (out 1997-2015); AAPL, AMD -> AMD's history starts 2016, so only AAPL
    assert cov["by_year"]["2005"] == {"members": 2, "with_data": 1, "coverage": 0.5}
    # 2020: AAL, AAPL, AMD, BRK.B, META -> AAPL, AMD, META
    assert cov["by_year"]["2020"] == {"members": 5, "with_data": 3, "coverage": 0.6}
    assert cov["coverage_since_2010"] == round(float(np.mean(
        [cov["by_year"][str(y)]["coverage"] for y in range(2010, 2027)])), 4)
    assert pit.replay_symbols(["spy", "AAPL"], cov) == ["AAPL", "AMD", "META", "SPY"]
    # cached for a month, recomputed on demand
    cov2 = pit.coverage(m, loader=lambda t: (_ for _ in ()).throw(RuntimeError("no")), now=NOW, cache_path=cache)
    assert cov2["available"] == cov["available"]
    cov3 = pit.coverage(m, loader=lambda t: (_ for _ in ()).throw(RuntimeError("no")), now=NOW, cache_path=cache,
                        refresh=True)
    assert cov3["available"] == []


# ------------------------------------------------------------- replay mask

def _walk(rows: int, seed: int, start: str) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 * np.cumprod(1 + rng.normal(0.0004, 0.02, rows))
    dates = pd.bdate_range(start, periods=rows)
    return pd.DataFrame({"date": dates.strftime("%Y-%m-%d"), "open": close * (1 + rng.normal(0, 0.003, rows)),
                         "high": close * 1.02, "low": close * 0.98, "close": close, "volume": 1e6})


def test_an_entry_outside_membership_never_trades():
    frames = {"SPY": _walk(600, 1, "2022-01-03"), "AAPL": _walk(600, 2, "2022-01-03")}
    replays = {s: gate_replay.primary.replay_signals(f) for s, f in frames.items()}
    unmasked = gate_replay.simulate("moving_average_crossover", frames, frames["SPY"], {}, replays)
    aapl = unmasked[unmasked["symbol"] == "AAPL"]
    assert len(aapl) > 2, "the fixture needs AAPL trades to mask"
    cutoff = pd.Timestamp(aapl["decision_date"].iloc[len(aapl) // 2])
    frame = pd.DataFrame({"ticker": ["AAPL"], "start_date": ["1990-01-01"], "end_date": [str(cutoff.date())]})
    membership = pit.Membership.from_frame(pit.parse_membership(frame))
    masked = gate_replay.simulate("moving_average_crossover", frames, frames["SPY"], {}, replays,
                                  universe=membership.mask)
    kept = masked[masked["symbol"] == "AAPL"]
    assert 0 < len(kept) < len(aapl)
    assert (pd.to_datetime(kept["decision_date"]) < cutoff).all()
    # the ETF is untouched; a symbol with no membership never trades
    assert len(masked[masked["symbol"] == "SPY"]) == len(unmasked[unmasked["symbol"] == "SPY"])
    none = gate_replay.simulate("moving_average_crossover", frames, frames["SPY"], {}, replays,
                                universe=lambda symbol, dates: np.zeros(len(dates), dtype=bool))
    assert none.empty
    # discovery's evaluator honours the same mask
    candidate = next(c for c in sd.candidate_library(["SPY", "AAPL"]) if c["universe"] == "single_stocks")
    free = sd.evaluate_candidate(candidate, frames)["trades"]
    held = sd.evaluate_candidate(candidate, frames, universe=lambda s, d: np.zeros(len(d), dtype=bool))["trades"]
    assert held.empty and len(free) >= 0


# ---------------------------------------------------------- the setting

def test_replay_universe_builds_the_context_from_the_given_loaders():
    m = _membership()
    histories = {"SPY": _walk(300, 5, "2024-01-02"), "AAPL": _walk(300, 6, "2024-01-02"), "AMD": _walk(300, 7, "2024-01-02")}

    def loader(ticker):
        return histories[ticker]

    context = pit.replay_universe(["spy", "AAPL"], loader=loader, earnings_loader=lambda s: pd.DataFrame(), now=NOW,
                                  membership=m)
    assert context.symbols == ["AAPL", "AMD", "SPY"] and context.loader is loader
    assert context.mask("AMD", ["2015-01-02", "2020-01-02"]).tolist() == [False, True]
    summary = context.summary()
    assert summary["mode"] == "point_in_time" and summary["symbols"] == 3
    assert summary["available_members"] == 2 and summary["missing_members"] == 3  # AAL, BRK.B, META: no history


def test_engine_and_discovery_use_the_point_in_time_universe_when_enabled(db, monkeypatch):  # noqa: F811
    m = _membership()
    histories = {"SPY": _walk(400, 3, "2024-01-02"), "AAPL": _walk(400, 4, "2024-01-02"), "AMD": _walk(400, 8, "2024-01-02")}
    monkeypatch.setattr(settings, "paper_execution_symbols", ["SPY", "AAPL"])
    monkeypatch.setattr(settings, "experiment_engine_max_experiments", 4)
    monkeypatch.setattr(settings, "replay_point_in_time_universe", True)
    monkeypatch.setattr(pit, "load_membership", lambda **kwargs: m)
    seen = {}

    def simulate(strategy, frames, spy, setup, replays, vix=None, universe=None):
        seen["symbols"], seen["universe"] = sorted(frames), universe
        return gate_replay.simulate(strategy, frames, spy, setup, replays, vix=vix, universe=universe)

    loaders = {"loader": lambda symbol: histories[symbol], "earnings_loader": lambda symbol: pd.DataFrame(),
               "vix_loader": lambda start: pd.DataFrame(columns=["date", "close"])}
    _strategy(db, "channel_breakout")
    _repeat_market_losses(db, "channel_breakout")
    result = engine.run_experiment_engine(db, now=NOW, simulate=simulate, **loaders)
    assert result["status"] == "complete" and result["planned"] == 1 and result["errors"] == []
    assert seen["symbols"] == ["AAPL", "AMD", "SPY"] and seen["universe"] is not None
    assert seen["universe"]("AMD", ["2015-01-02"]).tolist() == [False]

    discovery = sd.run_strategy_discovery(db, now=NOW, loader=loaders["loader"], earnings_loader=loaders["earnings_loader"])
    assert discovery["status"] == "complete" and discovery["universe"]["mode"] == "point_in_time"
    assert discovery["universe"]["symbols"] == 3 and discovery["missing_symbols"] == []


def test_engine_and_discovery_keep_todays_list_when_the_setting_is_off(db, monkeypatch):  # noqa: F811
    histories = {"SPY": _walk(400, 3, "2024-01-02"), "AAPL": _walk(400, 4, "2024-01-02")}
    monkeypatch.setattr(settings, "paper_execution_symbols", ["SPY", "AAPL"])
    monkeypatch.setattr(settings, "replay_point_in_time_universe", False)
    seen = {}

    def simulate(strategy, frames, spy, setup, replays, vix=None, **kwargs):
        seen["symbols"], seen["kwargs"] = sorted(frames), kwargs
        return gate_replay.simulate(strategy, frames, spy, setup, replays, vix=vix)

    loaders = {"loader": lambda symbol: histories[symbol], "earnings_loader": lambda symbol: pd.DataFrame(),
               "vix_loader": lambda start: pd.DataFrame(columns=["date", "close"])}
    with patch.object(pit, "load_membership", side_effect=AssertionError("must not load membership")):
        _strategy(db, "channel_breakout")
        _repeat_market_losses(db, "channel_breakout")
        result = engine.run_experiment_engine(db, now=NOW, simulate=simulate, **loaders)
        assert result["status"] == "complete" and result["planned"] == 1
        assert seen["symbols"] == ["AAPL", "SPY"] and seen["kwargs"] == {}
        discovery = sd.run_strategy_discovery(db, now=NOW, loader=loaders["loader"],
                                              earnings_loader=loaders["earnings_loader"])
    assert discovery["universe"] == {"mode": "today", "symbols": 2}


def test_point_in_time_falls_back_to_todays_list_without_a_membership_file(db, monkeypatch):  # noqa: F811
    histories = {"SPY": _walk(400, 3, "2024-01-02"), "AAPL": _walk(400, 4, "2024-01-02")}
    monkeypatch.setattr(settings, "paper_execution_symbols", ["SPY", "AAPL"])
    monkeypatch.setattr(settings, "replay_point_in_time_universe", True)
    with patch.object(pit, "load_membership", side_effect=FileNotFoundError("no cache")):
        result = sd.run_strategy_discovery(db, now=NOW, loader=lambda s: histories[s],
                                           earnings_loader=lambda s: pd.DataFrame())
    assert result["status"] == "complete" and result["universe"]["mode"] == "today"
    assert "FileNotFoundError" in result["universe"]["fallback"]


def test_discovered_scope_is_trimmed_to_the_tradeable_list():
    row = {"id": "abc", "family": "f", "universe": "single_stocks", "symbols": ["AAPL"], "evaluated_symbols": 700,
           "rule": {}, "stop_loss_pct": 0.03, "take_profit_pct": 0.06, "max_holding_sessions": 5,
           "gate": {}, "discovery": {}, "post_period": {"expectancy_bps": 1.0}, "holdout": {}}
    params = sd.strategy_parameters(row, now=NOW)
    assert params["symbol_scope"] == ["AAPL"] and params["discovery"]["evaluated_symbols"] == 700
