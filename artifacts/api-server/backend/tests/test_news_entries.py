"""News tone as an entry condition: tone series, backfill, rule features, candidates, registry.

Nothing reaches Alpaca: news pages are injected and the paper-path series is pinned.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.core.config import settings
from app.models import NewsArticle
from app.models import StockTradeAttribution
from app.services import news_discovery as nd
from app.services import news_history as nh
from app.services import strategy_discovery as sd
from app.services.strategies import rules
from app.services.strategies.news_tone import DEFAULT_RULE, NewsToneStrategy
from app.services.strategies.registry import get_strategy, is_known_strategy
from app.services.strategies.rules import RuleStrategy
from tests.test_loss_feedback_loop import db  # noqa: F401  (fixture)
from tests.test_meta_label import _walk

UTC = timezone.utc
NOW = datetime(2026, 11, 6, 15, 0, tzinfo=UTC)  # Friday 10:00 ET
POSITIVE_RULE = {"entry": [{"f": "news_tone_1", "op": ">=", "v": 0.5}, {"f": "news_articles_1", "op": ">=", "v": 2}],
                 "exit": [{"f": "dist_sma_5", "op": ">", "v": 0}]}


@pytest.fixture(autouse=True)
def _clean():
    nh.clear_cache()
    yield
    nh.clear_cache()


def _stamp(day: str, hour: int, minute: int = 0) -> datetime:
    """A New York wall-clock time as UTC."""
    return datetime.fromisoformat(f"{day}T{hour:02d}:{minute:02d}:00").replace(tzinfo=nh.ET).astimezone(UTC)


# ------------------------------------------------------------ sessions and series

def test_a_story_belongs_to_the_first_session_that_can_trade_on_it():
    assert nh.session_day(_stamp("2026-11-05", 9, 0)).isoformat() == "2026-11-05"  # pre-market Thursday
    assert nh.session_day(_stamp("2026-11-05", 15, 59)).isoformat() == "2026-11-05"
    assert nh.session_day(_stamp("2026-11-05", 16, 0)).isoformat() == "2026-11-06"  # at the close: next session
    assert nh.session_day(_stamp("2026-11-06", 17, 0)).isoformat() == "2026-11-09"  # Friday evening: Monday
    assert nh.session_day(_stamp("2026-11-07", 12, 0)).isoformat() == "2026-11-09"  # Saturday
    assert nh.session_day(datetime(2026, 11, 5, 20, 0)).isoformat() == "2026-11-05"  # naive = UTC, 15:00 ET


def test_tone_series_from_stored_articles_ignores_other_sources_and_routine_notes(db):  # noqa: F811
    articles = [
        {"id": 1, "symbols": ["PLTR"], "headline": "Palantir soars after record contract wins",
         "created_at": _stamp("2026-11-04", 10, 0).isoformat()},
        {"id": 2, "symbols": ["PLTR"], "headline": "Morgan Stanley Maintains Overweight on Palantir, Raises Price Target",
         "created_at": _stamp("2026-11-04", 11, 0).isoformat()},  # routine: counted, not a view
        {"id": 3, "symbols": ["PLTR"], "headline": "Palantir plunges as deliveries miss",
         "created_at": _stamp("2026-11-04", 18, 0).isoformat()},  # after the close: next session
        {"id": 4, "symbols": ["PLTR", "AMD", "INTC", "MU"], "headline": "Chip stocks rally on record demand",
         "created_at": _stamp("2026-11-05", 10, 0).isoformat()},  # roundup: never stored
    ]
    assert nd.store_articles(db, articles) == 3
    db.add(NewsArticle(symbol="PLTR", published_at=_stamp("2026-11-04", 12, 0).replace(tzinfo=None),
                       source="mock_news", title="Synthetic", summary="", sentiment_score=Decimal("1")))
    db.flush()
    series = nh.tone_series("PLTR", db)
    assert series["date"].tolist() == ["2026-11-04", "2026-11-05"]
    assert series["articles"].tolist() == [2, 1] and series["directional"].tolist() == [1, 1]
    assert series["tone"].tolist() == [1.0, -1.0]
    assert nh.tone_series("MSFT", db).empty
    assert nh.stored_tone_series(db, "PLTR").equals(series)


def test_attach_rolls_a_holiday_session_forward_and_never_backward():
    series = pd.DataFrame({"date": ["2026-11-25", "2026-11-26", "2026-11-27", "2026-12-01"],
                           "tone": [1.0, -1.0, 0.0, 0.5], "articles": [1, 2, 3, 4], "directional": [1, 1, 0, 2]})
    frame = pd.DataFrame({"date": ["2026-11-24", "2026-11-25", "2026-11-27", "2026-11-30"],
                          "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0})
    out = nh.attach_tone(frame, series)
    # Thanksgiving (11-26) has no bar: its stories join 11-27 with directional weighting.
    assert out["news_articles"].tolist() == [0.0, 1.0, 5.0, 0.0]
    assert out["news_tone"].tolist() == [0.0, 1.0, -1.0, 0.0]
    assert nh.attach_tone(frame, None)["news_tone"].tolist() == [0.0] * 4
    assert nh.attach_tone(frame, nh.empty_series())["news_articles"].tolist() == [0.0] * 4


# ------------------------------------------------------------ backfill

def _pages_for(stories: dict[str, list[tuple[datetime, float, str]]], *, fail_after: int | None = None,
               log: list | None = None):
    """A fake ``alpaca_news_pages``: every story for the queried symbols in the window, 50 a page, newest first."""
    calls = {"n": 0}

    def fetch_pages(symbols, start, end, *, max_pages):
        items = []
        for symbol, rows in stories.items():
            for published, tone, headline in rows:
                if start <= published <= end and symbol in symbols:
                    items.append({"id": f"{symbol}-{published.isoformat()}", "symbols": [symbol],
                                  "headline": headline, "created_at": published.isoformat()})
        items.sort(key=lambda item: item["created_at"], reverse=True)
        pages = [items[i:i + nh.PAGE_SIZE] for i in range(0, len(items), nh.PAGE_SIZE)] or [[]]
        for index, page in enumerate(pages):
            if index >= max_pages:
                raise nh.BackfillStopped("request budget exhausted inside a window")
            calls["n"] += 1
            if fail_after is not None and calls["n"] > fail_after:
                raise nh.BackfillStopped("Alpaca news rate limit reached")
            if log is not None:
                log.append((tuple(symbols), start, end))
            yield page
    return fetch_pages


def test_backfill_walks_back_in_weekly_windows_within_the_budget_and_resumes(tmp_path):
    stories = {"AAPL": [(_stamp("2026-11-05", 10), 1.0, "Apple beats on record growth")]
               + [(_stamp(day, 10), -1.0, "Apple misses") for day in ("2026-10-27", "2026-10-20", "2026-10-13")],
               "MSFT": [(_stamp("2026-10-30", 17), 1.0, "Microsoft wins record contract")]}  # Friday evening
    # One story a page: the first window (two stories) needs two requests; a budget of one drops it whole.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(nh, "PAGE_SIZE", 1)
        partial = nh.backfill(["AAPL", "MSFT"], budget=1, data_dir=tmp_path, now=NOW,
                              fetch_pages=_pages_for(stories), start=nh.HISTORY_START)
    assert partial["stopped"] == "request budget exhausted inside a window" and partial["windows"] == 0
    assert nh.load_coverage(tmp_path) == {} and nh.cached_tone_series("AAPL", tmp_path).empty
    # One request per weekly window: a budget of two covers two weeks and stops before the third.
    log = []
    result = nh.backfill(["AAPL", "MSFT"], budget=2, data_dir=tmp_path, now=NOW,
                         fetch_pages=_pages_for(stories, log=log), start=nh.HISTORY_START)
    assert result["stopped"] == "request budget exhausted" and result["windows"] == 2
    assert result["requests"] == 2 and result["complete"] == []
    assert log[0][0] == ("AAPL", "MSFT") and log[0][2] < _stamp("2026-11-07", 0)  # ends before today's midnight ET
    coverage = nh.load_coverage(tmp_path)
    assert coverage["AAPL"] == coverage["MSFT"] | {"requests": coverage["AAPL"]["requests"]}
    assert coverage["AAPL"]["start"] == "2026-10-23" and coverage["AAPL"]["end"] == "2026-11-06"
    aapl = nh.cached_tone_series("AAPL", tmp_path)
    assert aapl["date"].tolist() == ["2026-10-27", "2026-11-05"] and aapl["tone"].tolist() == [-1.0, 1.0]
    msft = nh.cached_tone_series("MSFT", tmp_path)
    assert msft["date"].tolist() == ["2026-11-02"] and msft["articles"].tolist() == [1]  # Monday session

    # The next run resumes from the recorded coverage and finishes the archive.
    resumed = nh.backfill(["AAPL", "MSFT"], budget=50, data_dir=tmp_path, now=NOW,
                          fetch_pages=_pages_for(stories), start=datetime(2026, 10, 1).date())
    assert resumed["stopped"] is None and resumed["complete"] == ["AAPL", "MSFT"]
    assert nh.load_coverage(tmp_path)["AAPL"]["start"] == "2026-10-01"
    assert nh.cached_tone_series("AAPL", tmp_path)["date"].tolist() == ["2026-10-13", "2026-10-20", "2026-10-27",
                                                                         "2026-11-05"]
    # Fully covered and current: nothing to fetch, no request spent.
    idle = nh.backfill(["AAPL", "MSFT"], budget=50, data_dir=tmp_path, now=NOW, fetch_pages=_pages_for(stories),
                       start=datetime(2026, 10, 1).date())
    assert idle["requests"] == 0 and idle["stopped"] is None

    # A week later the cache catches up forward to yesterday before digging further back.
    later = NOW + timedelta(days=7)
    stories["AAPL"].append((_stamp("2026-11-10", 10), 1.0, "Apple upgraded"))
    forward = nh.backfill(["AAPL", "MSFT"], budget=50, data_dir=tmp_path, now=later, fetch_pages=_pages_for(stories),
                          start=datetime(2026, 10, 1).date())
    assert forward["windows"] == 1 and nh.load_coverage(tmp_path)["AAPL"]["end"] == "2026-11-13"
    assert "2026-11-10" in nh.cached_tone_series("AAPL", tmp_path)["date"].tolist()


def test_backfill_stops_at_the_rate_limit_and_records_nothing_partial(tmp_path):
    stories = {"AAPL": [(_stamp("2026-11-05", 10), 1.0, "Apple beats on record growth")]}
    result = nh.backfill(["AAPL"], budget=10, data_dir=tmp_path, now=NOW, fetch_pages=_pages_for(stories, fail_after=0))
    assert result["stopped"] == "Alpaca news rate limit reached" and result["windows"] == 0
    assert nh.load_coverage(tmp_path) == {} and nh.cached_tone_series("AAPL", tmp_path).empty
    assert nh.backfill(["AAPL"], budget=0, data_dir=tmp_path, now=NOW)["requests"] == 0


def test_stored_articles_fill_only_the_days_the_cache_does_not_cover(db, tmp_path):  # noqa: F711, F811
    stories = {"AAPL": [(_stamp("2026-11-05", 10), 1.0, "Apple beats on record growth")]}
    nh.backfill(["AAPL"], budget=10, data_dir=tmp_path, now=NOW, fetch_pages=_pages_for(stories))
    nd.store_articles(db, [
        {"id": 1, "symbols": ["AAPL"], "headline": "Apple beats on record growth",  # inside the span: cached already
         "created_at": _stamp("2026-11-05", 10).isoformat()},
        {"id": 2, "symbols": ["AAPL"], "headline": "Apple plunges on recall",  # today, after the cached span
         "created_at": _stamp("2026-11-06", 9).isoformat()},
    ])
    series = nh.tone_series("AAPL", db, tmp_path)
    assert series["date"].tolist() == ["2026-11-05", "2026-11-06"]
    assert series["articles"].tolist() == [1, 1] and series["tone"].tolist() == [1.0, -1.0]
    assert nh.coverage_summary(["AAPL", "MSFT"], tmp_path)["AAPL"]["sessions_with_news"] == 1
    assert nh.coverage_summary(["MSFT"], tmp_path)["MSFT"]["start"] is None


def test_discovery_run_spends_the_backfill_budget_on_the_single_stocks(db, monkeypatch):  # noqa: F811
    from tests.test_news_discovery import NEWS, _asset, _importer

    calls = []
    monkeypatch.setattr(settings, "news_discovery_enabled", True)
    monkeypatch.setattr(settings, "news_history_backfill_requests_per_run", 7)
    monkeypatch.setattr(nh, "backfill", lambda symbols, **kw: calls.append((symbols, kw["budget"])) or {
        "requests": 7, "windows": 1, "stories": 3, "stopped": None, "symbols": len(symbols), "complete": [],
        "coverage": {s: {"start": "2026-10-30", "end": "2026-11-06"} for s in symbols}})
    payload = nd.run_news_discovery(db, now=NOW, fetch_news=lambda start, end: NEWS, lookup_asset=_asset,
                                    import_prices=_importer({}))
    assert payload["history"] == {"requests": 7, "windows": 1, "stories": 3, "stopped": None,
                                  "symbols": len(calls[0][0]), "complete": 0, "earliest": "2026-10-30"}
    symbols, budget = calls[0]
    assert budget == 7 and "AAPL" in symbols and "PLTR" in symbols and "SPY" not in symbols and "XLK" not in symbols
    monkeypatch.setattr(settings, "news_history_backfill_requests_per_run", 0)
    assert nd.run_news_discovery(db, now=NOW + timedelta(hours=1), fetch_news=lambda s, e: NEWS, lookup_asset=_asset,
                                 import_prices=_importer({}))["history"] is None
    nd.clear_cache()


# ------------------------------------------------------------ rule features

def _tone_series(prices: pd.DataFrame, seed: int = 3) -> pd.DataFrame:
    """Synthetic tone on a third of the sessions, keyed by the frame's own dates."""
    rng = np.random.default_rng(seed)
    dates = prices["date"].to_numpy()
    pick = rng.random(len(dates)) < 0.34
    directional = rng.integers(0, 4, pick.sum())
    tone = np.where(directional > 0, rng.choice([-1.0, -0.5, 0.5, 1.0], pick.sum()), 0.0)
    return pd.DataFrame({"date": dates[pick], "tone": tone, "articles": directional + rng.integers(1, 3, pick.sum()),
                         "directional": directional})


def test_news_features_are_causal_and_default_to_zero():
    prices = _walk(rows=600, seed=4)
    bare = {name: rules.feature(prices, name) for name in ("news_tone", "news_tone_1", "news_articles_1")}
    assert all((series == 0).all() for series in bare.values())
    frame = nh.attach_tone(prices, _tone_series(prices))
    assert (rules.feature(frame, "news_tone_1").to_numpy()[1:] == frame["news_tone"].to_numpy()[:-1]).all()
    assert rules.feature(frame, "news_tone_1").iloc[0] == 0 and rules.feature(frame, "news_articles_3").iloc[2] == 0
    assert (rules.feature(frame, "news_articles").to_numpy() == frame["news_articles"].to_numpy()).all()
    noisy = frame.copy()
    noisy.loc[401:, ["news_tone", "news_articles"]] = 1.0
    for name in ("news_tone", "news_tone_1", "news_articles", "news_articles_1", "news_tone_5"):
        np.testing.assert_array_equal(rules.feature(frame, name).to_numpy()[:401],
                                      rules.feature(noisy, name).to_numpy()[:401], err_msg=name)
    assert rules.uses_news(POSITIVE_RULE) and not rules.uses_news({"entry": [{"f": "rsi_2", "op": "<", "v": 5}]})
    assert rules.rule_lookback(POSITIVE_RULE) == 6  # dist_sma_5 is the longest
    assert rules.validate_rule(POSITIVE_RULE) is POSITIVE_RULE
    with pytest.raises(ValueError):
        rules.validate_rule({"entry": [{"f": "news_tone_1_2", "op": ">", "v": 0}]})


def test_rule_strategy_attaches_the_runtime_tone_series_and_matches_the_vectorised_rule(monkeypatch):
    prices = _walk(rows=400, seed=9)
    series = _tone_series(prices)
    nh.set_runtime_tone("AAPL", series)
    strategy = get_strategy("rule_news1", {"rule": POSITIVE_RULE, "max_holding_sessions": 5})
    frame = nh.attach_tone(prices, series)
    entries = rules.entry_signals(frame, POSITIVE_RULE)
    assert entries.any()
    for t in [*np.flatnonzero(entries)[:8], 300, 350, 399]:
        signal = strategy.generate_signal("AAPL", prices.iloc[:t + 1])
        assert (signal.action == "BUY") == entries[t], t
        assert signal.features["news_tone_1"] == frame["news_tone"].iloc[t - 1]
    # Without a store the rule stays neutral; with an empty series it just never fires.
    monkeypatch.setattr(nh, "runtime_tone", lambda symbol: None)
    nh.set_runtime_tone("AAPL", None)
    neutral = strategy.generate_signal("AAPL", prices)
    assert neutral.action == "HOLD" and "news tone history unavailable" in neutral.reason
    monkeypatch.setattr(nh, "runtime_tone", lambda symbol: nh.empty_series())
    assert strategy.generate_signal("AAPL", prices).action == "HOLD"


def test_runtime_tone_caches_per_process_and_fails_soft(monkeypatch):
    class _Boom:
        def __call__(self):
            raise RuntimeError("no database")

    monkeypatch.setattr("app.db.session.SessionLocal", _Boom())
    assert nh.runtime_tone("AAPL") is None
    nh.set_runtime_tone("AAPL", pd.DataFrame({"date": ["2026-11-05"], "tone": [1.0], "articles": [2]}))
    pinned = nh.runtime_tone("AAPL")
    assert pinned["directional"].tolist() == [1] and pinned["tone"].tolist() == [1.0]


# ------------------------------------------------------------ candidates

def test_news_candidates_validate_and_evaluate_on_synthetic_frames():
    symbols = ["SPY", "QQQ", "XLK", "AAPL", "MSFT", "NVDA"]
    library = sd.candidate_library(symbols)
    news = [row for row in library if row["family"] in sd.NEWS_FAMILIES]
    assert len(news) == 12 and {row["universe"] for row in news} == {"single_stocks"}
    assert {row["family"] for row in news} == sd.NEWS_FAMILIES
    assert all(rules.uses_news(row["rule"]) and sd.paper_executable(row) for row in news)
    # The family counts as trials; an ETF-only universe carries none of them.
    assert len(library) == len(sd.candidate_library(["SPY", "QQQ", "XLK"])) + 12 + sum(
        1 for row in library if row["universe"] == "single_stocks" and row["family"] not in sd.NEWS_FAMILIES)
    frames = {s: nh.attach_tone(_walk(rows=1600, seed=40 + i, start="2014-01-02"), _tone_series(_walk(rows=1600, seed=40 + i, start="2014-01-02"), seed=i))
              for i, s in enumerate(["AAPL", "MSFT", "NVDA"])}
    fired = 0
    for candidate in news:
        evaluation = sd.evaluate_candidate(candidate, frames)
        summary = sd.summarize_candidate(candidate, evaluation, pd.Timestamp("2019-01-01"))
        verdict = sd.gate(summary, trials=len(library), trial_sharpes=[0.0])
        assert verdict["tier"] in sd.TIER_ORDER and set(verdict["checks"]) >= sd.EXPLORE_CHECKS
        fired += len(evaluation["trades"])
        if candidate["family"] == "positive_news_trend":
            assert len(evaluation["trades"]) > 20 and set(evaluation["trades"]["symbol"]) <= set(frames)
    assert fired > 0
    # Without tone columns every news candidate is silent: zero trades, rejected, still a trial.
    bare = {s: f.drop(columns=["news_tone", "news_articles"]) for s, f in frames.items()}
    assert all(sd.evaluate_candidate(c, bare)["trades"].empty for c in news)
    result = sd.run_discovery(frames, frames.keys(), library=news, data_end="2026-11-06")
    assert result["trials"] == 12 and len(result["candidates"]) == 12


def test_load_frames_attaches_the_tone_series_to_single_stocks_only():
    prices = {s: _walk(rows=700, seed=50 + i, start="2023-01-02") for i, s in enumerate(["SPY", "AAPL"])}
    tone = _tone_series(prices["AAPL"])
    frames, missing = sd.load_frames(["SPY", "AAPL"], loader=lambda s: prices[s].copy(), start="2023-01-01",
                                     earnings_loader=lambda s: None, news_loader=lambda s: tone)
    assert missing == [] and "news_tone" in frames["AAPL"].columns and "news_tone" not in frames["SPY"].columns
    assert frames["AAPL"]["news_articles"].sum() == tone["articles"].sum()
    broken, _ = sd.load_frames(["AAPL"], loader=lambda s: prices[s].copy(), start="2023-01-01",
                               earnings_loader=lambda s: None, news_loader=lambda s: 1 / 0)
    assert (broken["AAPL"]["news_tone"] == 0).all()


# ------------------------------------------------------------ registry

def test_registry_resolves_the_news_tone_strategy_and_rule_slugs():
    strategy = get_strategy("news_tone", {"symbol_scope": ["AAPL"]})
    assert isinstance(strategy, NewsToneStrategy) and isinstance(strategy, RuleStrategy)
    assert strategy.slug == "news_tone" and strategy.daily_bar_execution and strategy.rule == DEFAULT_RULE
    assert rules.uses_news(strategy.rule) and strategy.exit_policy().max_holding_sessions == 5
    assert is_known_strategy("news_tone") and is_known_strategy("rule_abc123")
    custom = get_strategy("news_tone", {"rule": POSITIVE_RULE, "max_holding_sessions": 3})
    assert custom.rule == POSITIVE_RULE and custom.exit_policy().max_holding_sessions == 3
    discovered = get_strategy("rule_n1", {"rule": POSITIVE_RULE})
    assert type(discovered).__name__ == "RuleStrategy" and discovered.slug == "rule_n1"


# ------------------------------------------------------------ the record so far

def test_news_track_record_compares_news_found_and_core_trades(db):  # noqa: F811
    from app.services.audit import write_audit_log

    write_audit_log(db, event_type=nd.EVENT_TYPE, entity_type="asset", action="run_news_discovery",
                    status="complete", message="", payload={"admitted": [{"symbol": "PLTR"}], "holding": []})
    rows = [("PLTR", -0.02, "news_wrong"), ("PLTR", 0.03, None), ("PLTR", -0.01, "signal_wrong"),
            ("AAPL", 0.01, None), ("SPY", -0.02, "noise")]
    for index, (symbol, ret, cause) in enumerate(rows):
        db.add(StockTradeAttribution(
            source="scheduled_round_trip", source_key=f"round_trip:{index}", revision=1, attribution_sha256="0" * 64,
            strategy_type="rsi2_pullback", symbol=symbol, opened_at=NOW.replace(tzinfo=None) - timedelta(days=3),
            closed_at=NOW.replace(tzinfo=None) - timedelta(days=1), exit_cause="indicator",
            net_return=Decimal(str(ret)), gross_return=Decimal(str(ret)), net_pnl=Decimal(str(ret * 100)),
            costs_known=True, is_loss=ret <= 0, detail={"diagnosis": {"cause": cause}} if cause else {},
            paper_only=True))
    db.flush()
    record = nh.news_track_record(db, now=NOW)
    assert record["news_symbols_ever_admitted"] == ["PLTR"] and record["discovery_runs"] == 1
    assert record["news"]["trades"] == 3 and record["news"]["wins"] == 1 and record["core"]["trades"] == 2
    assert record["news_wrong"] == 1 and record["news_loss_causes"] == {"news_wrong": 1, "signal_wrong": 1}
    table = nh.track_record_table(record)
    assert "| News-found symbols | 3 | 1 |" in table and "| Core universe | 2 | 1 |" in table
    assert record["paper_only"] is True
