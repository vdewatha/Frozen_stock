"""Post-earnings drift and opening-gap-fill candidates for strategy discovery."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from app.services import earnings_history as eh
from app.services import strategy_discovery as sd
from app.services.strategies import rules
from app.services.strategies.registry import get_strategy
from app.services.triple_barrier import label_open_session

from tests.test_meta_label import _walk

UTC = timezone.utc
DRIFT = {"entry": [{"f": "earn_surprise", "op": ">", "v": 5}, {"f": "ret_1", "op": ">", "v": 0.0}], "exit": []}
GAP_FILL = {"timing": "open", "entry": [{"f": "dist_sma_200", "op": ">", "v": 0}, {"f": "gap", "op": "<", "v": -0.002},
                                        {"f": "gap", "op": ">", "v": -0.03}]}


def _events(*rows):
    return pd.DataFrame({"reported_at": [pd.Timestamp(stamp, tz=eh.ET) for stamp, _ in rows],
                         "surprise_pct": [value for _, value in rows]})


def _surprised(prices: pd.DataFrame, every: int = 25, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    days = prices["date"].iloc[::every]
    rows = [(f"{day} {'07:00' if i % 2 else '16:05'}", float(rng.normal(4, 8))) for i, day in enumerate(days)]
    return eh.attach_surprises(prices, _events(*rows))


def test_clean_surprises_drops_unusable_reports():
    raw = _events(("2024-01-10 16:00", 5.0), ("2024-04-10 16:00", np.nan), ("2024-07-10 16:00", 1088.0),
                  ("2024-10-10 00:00", 3.0), ("2025-01-10 16:00", 2.0), ("2025-01-10 16:00", 9.0),
                  ("2023-10-10 07:00", -4.0))
    clean = eh.clean_surprises(raw)
    assert [str(t.date()) for t in clean["reported_at"]] == ["2023-10-10", "2024-01-10"]
    assert clean["surprise_pct"].tolist() == [-4.0, 5.0]


def test_reports_land_on_the_first_session_that_traded_them():
    prices = _walk(rows=10, start="2024-01-08")  # Mon 8th .. Fri 19th
    events = _events(("2024-01-09 07:00", 1.0), ("2024-01-10 16:05", 2.0), ("2024-01-12 12:00", 3.0),
                     ("2024-01-13 10:00", 4.0), ("2024-01-19 16:00", 5.0))
    framed = eh.attach_surprises(prices, events)
    values = dict(zip(framed["date"], framed[eh.SURPRISE_COLUMN]))
    # before the open: same day; after the close: next session; past the end: dropped
    assert values["2024-01-09"] == 1.0 and values["2024-01-11"] == 2.0
    assert np.isnan(values["2024-01-10"]) and np.isnan(values["2024-01-12"])
    # Friday's intraday report and Saturday's both first trade on Monday: ambiguous, so neither counts.
    assert np.isnan(values["2024-01-15"]) and framed[eh.SURPRISE_COLUMN].notna().sum() == 2


def test_load_surprises_caches_and_survives_provider_failures(tmp_path):
    calls = []

    def fetch(symbol):
        calls.append(symbol)
        return _events(("2024-01-10 16:00", 5.0))

    now = datetime(2026, 11, 2, tzinfo=UTC)
    first = eh.load_surprises("AAPL", tmp_path, download=True, fetch=fetch, now=now)
    again = eh.load_surprises("AAPL", tmp_path, download=True, fetch=fetch, now=now)
    assert len(first) == len(again) == 1 and calls == ["AAPL"]

    def broken(symbol):
        raise RuntimeError("down")

    later = datetime(2027, 1, 1, tzinfo=UTC)
    assert len(eh.load_surprises("AAPL", tmp_path, download=True, fetch=broken, now=later)) == 1
    assert eh.load_surprises("MSFT", tmp_path, download=True, fetch=broken, now=later).empty
    eh.clear_cache()
    assert eh.runtime_surprises("MSFT", now=later, fetch=broken) is None


def test_drift_rule_matches_the_vectorised_rule_on_every_prefix():
    prices = _surprised(_walk(rows=520, seed=17))
    strategy = get_strategy("rule_pead", {"rule": DRIFT})
    entries = rules.entry_signals(prices, DRIFT)
    assert entries.sum() >= 3
    for t in range(260, 520):
        assert (strategy.generate_signal("AAPL", prices.iloc[:t + 1]).action == "BUY") == entries[t], t


def test_drift_rule_fetches_earnings_at_runtime_and_holds_when_unavailable():
    prices = _surprised(_walk(rows=400, seed=17))
    fire = int(np.flatnonzero(rules.entry_signals(prices, DRIFT))[-1])
    bare = prices.drop(columns=[eh.SURPRISE_COLUMN]).iloc[:fire + 1]
    events = eh.clean_surprises(_events(*[(f"{d} 07:00", v) for d, v in
                                          zip(prices["date"], prices[eh.SURPRISE_COLUMN]) if np.isfinite(v)]))
    strategy = get_strategy("rule_pead", {"rule": DRIFT})
    with patch.object(eh, "runtime_surprises", return_value=events):
        assert strategy.generate_signal("AAPL", bare).action == "BUY"
    with patch.object(eh, "runtime_surprises", return_value=None):
        held = strategy.generate_signal("AAPL", bare)
        assert held.action == "HOLD" and "earnings history unavailable" in held.reason
    # A frame with no earnings column at all (an ETF in discovery) never fires.
    assert not rules.entry_signals(bare, DRIFT).any()


def test_open_rules_use_the_gap_today_and_everything_else_from_yesterday():
    prices = _walk(rows=600, seed=4)
    entries = rules.entry_signals(prices, GAP_FILL)
    gap = rules.feature(prices, "gap").to_numpy()
    trend = rules.feature(prices, "dist_sma_200").shift(1).to_numpy()
    expected = (gap < -0.002) & (gap > -0.03) & (trend > 0)
    expected[: rules.rule_lookback(GAP_FILL) - 1] = False
    np.testing.assert_array_equal(entries, expected)
    assert entries.any()
    # Bar t's high, low and close are unknown at its open and cannot change the decision.
    t = int(np.flatnonzero(entries)[0])
    moved = prices.copy()
    moved.loc[t, ["high", "low", "close"]] = moved.loc[t, ["high", "low", "close"]] * [1.5, 0.5, 0.6]
    np.testing.assert_array_equal(rules.entry_signals(moved, GAP_FILL)[:t + 1], entries[:t + 1])


def test_open_rules_are_validated_and_never_fire_on_the_daily_paper_path():
    with pytest.raises(ValueError):
        rules.validate_rule({"timing": "open", "entry": [{"f": "rsi_2", "op": "<", "v": 10}]})
    with pytest.raises(ValueError):
        rules.validate_rule({**GAP_FILL, "exit": [{"f": "ret_1", "op": ">", "v": 0}]})
    with pytest.raises(ValueError):
        rules.validate_rule({**GAP_FILL, "timing": "noon"})
    prices = _walk(rows=600, seed=4)
    t = int(np.flatnonzero(rules.entry_signals(prices, GAP_FILL))[-1])
    signal = get_strategy("rule_gap", {"rule": GAP_FILL}).generate_signal("SPY", prices.iloc[:t + 1])
    assert signal.action == "HOLD" and "at the open" in signal.reason
    assert "BUY at the open" in rules.describe_rule(GAP_FILL)


def _bar(prev_close, open_, high, low, close):
    return pd.DataFrame({"date": ["2024-01-08", "2024-01-09"], "open": [prev_close, open_],
                         "high": [prev_close, high], "low": [prev_close, low], "close": [prev_close, close],
                         "volume": [1e6, 1e6]})


@pytest.mark.parametrize("bar, barrier, exit_raw", [
    (_bar(100, 99, 100.2, 98.8, 99.5), "take_profit", 100.0),   # gap filled
    (_bar(100, 99, 99.6, 97.0, 97.5), "stop_loss", None),        # stopped
    (_bar(100, 99, 99.8, 98.5, 99.4), "close", 99.4),            # neither: out at the close
    (_bar(100, 99, 100.5, 97.0, 99.0), "stop_loss", None),       # both touched: stop first
])
def test_open_session_labels(bar, barrier, exit_raw):
    labels = label_open_session(bar, [1], stop_loss_pct=0.015)
    row = labels.iloc[0]
    assert row["barrier"] == barrier and row["entry_idx"] == row["exit_idx"] == 1 and row["holding_days"] == 0
    assert row["entry_price"] > 99  # costs on the way in
    if exit_raw is not None:
        assert row["gross_return"] == pytest.approx(exit_raw / 99 - 1)
    else:
        assert row["gross_return"] == pytest.approx(row["entry_price"] * (1 - 0.015) / 99 - 1)
    assert row["net_return"] < row["gross_return"]
    # No gap below the prior close: nothing to fill.
    assert label_open_session(_bar(100, 100.5, 101, 100, 100.8), [1], stop_loss_pct=0.015).empty


def test_library_scopes_the_new_families_and_evaluates_gap_fills_same_day():
    symbols = ["SPY", "QQQ", "XLK", "AAPL", "MSFT"]
    library = sd.candidate_library(symbols)
    drift = [c for c in library if c["family"] == "earnings_drift"]
    gaps = [c for c in library if c["family"] == "opening_gap_fill"]
    assert drift and {c["universe"] for c in drift} == {"single_stocks"}
    assert {c["universe"] for c in gaps} == {"index_etfs", "sector_etfs", "single_stocks"}
    frames = {s: _walk(rows=900, seed=40 + i) for i, s in enumerate(["SPY", "QQQ"])}
    candidate = next(c for c in gaps if c["universe"] == "index_etfs")
    trades = sd.evaluate_candidate(candidate, frames)["trades"]
    assert len(trades) and (trades["entry_idx"] == trades["exit_idx"]).all()
    assert set(trades["barrier"]) <= {"take_profit", "stop_loss", "close"}


def test_open_timed_candidates_are_reported_but_never_admitted():
    row = {"rule": GAP_FILL, "gate": {"tier": "proven"}}
    assert not sd.paper_executable(row) and not sd.admissible(row, require_proven=False)
    assert sd.admissible({"rule": DRIFT, "gate": {"tier": "explore"}}, require_proven=False)


def test_load_frames_attaches_earnings_to_single_stocks_only():
    prices = _walk(rows=400, seed=2)
    seen = []

    def earnings(symbol):
        seen.append(symbol)
        return _events((f"{prices['date'].iloc[300]} 07:00", 6.0))

    frames, missing = sd.load_frames(["SPY", "AAPL"], loader=lambda s: prices.copy(), start="2000-01-01",
                                     earnings_loader=earnings)
    assert seen == ["AAPL"] and missing == []
    assert frames["AAPL"][eh.SURPRISE_COLUMN].notna().sum() == 1
    assert eh.SURPRISE_COLUMN not in frames["SPY"].columns
