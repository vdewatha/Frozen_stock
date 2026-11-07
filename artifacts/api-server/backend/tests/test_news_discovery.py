"""News symbol discovery and one position per symbol.

Nothing reaches Alpaca: news, asset lookups and daily imports are injected.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pandas as pd
import pytest

from app.core.config import settings
from app.models import Asset, AuditLog, NewsArticle
from app.models.stock_paper import StockPaperPosition
from app.services import news_discovery as nd
from app.services.market_data import upsert_prices
from tests.test_loss_feedback_loop import db  # noqa: F401  (fixture)
from tests.test_stock_paper_autotrader import _ModelDb, _run

UTC = timezone.utc
NOW = datetime.now(UTC).replace(microsecond=0)


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(settings, "news_discovery_enabled", True)
    # The tone-history backfill is tested in test_news_entries.py; nothing here may call Alpaca.
    monkeypatch.setattr(settings, "news_history_backfill_requests_per_run", 0)
    nd.clear_cache()
    yield
    nd.clear_cache()


def _article(id_, symbols, headline, summary="", hours_ago=1):
    return {"id": id_, "symbols": symbols, "headline": headline, "summary": summary, "source": "benzinga",
            "url": f"https://news.example/{id_}", "created_at": (NOW - timedelta(hours=hours_ago)).isoformat()}


NEWS = [
    _article(1, ["PLTR"], "Palantir soars after record contract wins"),
    _article(2, ["PLTR"], "Palantir beats estimates, raises outlook"),
    _article(3, ["PLTR", "MSFT"], "Palantir and Microsoft expand partnership with strong growth"),
    _article(4, ["SOFI"], "SoFi jumps on record member growth"),
    _article(5, ["SOFI"], "SoFi upgraded to outperform"),
    _article(6, ["RIVN"], "Rivian plunges as deliveries miss"),
    _article(7, ["RIVN"], "Rivian recall widens"),
    _article(8, ["CRWD"], "CrowdStrike surges on strong guidance"),  # only one story
    _article(9, ["AAPL"], "Apple beats on record iPhone growth"),  # core symbol
    _article(10, ["AAPL"], "Apple upgraded on strong services"),
    _article(11, ["NVDA", "AMD", "INTC", "MU"], "Chip stocks rally on record demand"),  # roundup
    _article(12, ["NVDA", "AMD", "INTC", "MU"], "Chip stocks surge again"),
]


def test_ranking_needs_several_focused_positive_stories_outside_the_universe():
    ranked = nd.rank_candidates(NEWS, exclude={"AAPL", "MSFT"})
    assert [row["symbol"] for row in ranked] == ["PLTR", "SOFI"]
    pltr = ranked[0]
    assert pltr["articles"] == 3 and pltr["positive"] == 3 and pltr["avg_sentiment"] == 1.0
    assert pltr["headlines"][0]["headline"] == "Palantir soars after record contract wins"
    # Negative news (RIVN), one story (CRWD), core symbols and roundups never qualify.
    assert {"RIVN", "CRWD", "AAPL", "NVDA", "INTC"}.isdisjoint(row["symbol"] for row in ranked)
    # A repeated article id counts once.
    assert nd.rank_candidates(NEWS[:1] * 3, exclude=(), min_articles=2) == []


@pytest.mark.parametrize("headline, tone", [
    ("Barclays Maintains Underweight on Trex Co, Raises Price Target to $46", 0.0),
    ("BofA Securities Upgrades Trex to Buy and Raises Price Target", 1.0),
    ("Morgan Stanley Downgrades Rivian to Underweight", -1.0),
    ("Goldman Initiates Coverage On SoFi With Buy Rating", 0.5),
    ("Jefferies Initiates Coverage On Wayfair With Underperform Rating", -0.5),
    ("Deutsche Bank Adjusts Trex Price Target to $70", 0.0),
    ("Duolingo Insider Executes Share Sale", 0.0),
    ("eGain Approves Fiscal 2026 Variable Cash Compensation for Executives", 0.0),
    ("Gray Media, Inc. Declares Quarterly Cash Dividend", 0.0),
    ("eGain Q1 2027 Earnings Call Transcript", 0.0),
    ("Tanger Reports Q3 Results and Raises Full-Year Guidance", None),
])
def test_analyst_notes_are_scored_by_their_rating_action(headline, tone):
    assert nd.analyst_note_sentiment(headline) == tone


def test_routine_notes_count_as_coverage_but_not_tone():
    news = [_article(20, ["TREX"], "Barclays Maintains Underweight on Trex Co, Raises Price Target to $46"),
            _article(21, ["TREX"], "BofA Securities Upgrades Trex to Buy"),
            _article(24, ["TREX"], "Trex beats estimates and raises guidance"),
            _article(22, ["W"], "Barclays Maintains Equal-Weight on Wayfair, Raises Price Target"),
            _article(23, ["W"], "Wells Fargo Raises Price Target on Wayfair")]
    ranked = nd.rank_candidates(news, exclude=())
    assert [(row["symbol"], row["articles"], row["avg_sentiment"]) for row in ranked] == [("TREX", 3, 1.0)]


def test_one_directional_story_is_not_enough():
    # OKLO, 2026-11-06: one "sales beat" next to routine filings.
    news = [_article(30, ["OKLO"], "Oklo Q3 Cash, Equivalents & Marketable Securities $3.13B"),
            _article(31, ["OKLO"], "Oklo Q3 GAAP EPS $(0.22) vs $(0.20) YoY; Sales $1.86M Beat $1.02M Estimate")]
    assert nd.rank_candidates(news, exclude=()) == []
    assert [row["symbol"] for row in nd.rank_candidates(news, exclude=(), min_directional=1)] == ["OKLO"]


@pytest.mark.parametrize("text, sign", [
    # MCHP, 2026-11-06: admitted at +1.0 because "fell" and "cut" were not listed.
    ("Microchip Technology Analysts Slash Their Forecasts After Q2 Results. Microchip beat Q2 earnings and sales "
     "estimates but guided Q3 below forecasts. Shares fell 2.1%; analysts cut price targets.", -1),
    ("These Analysts Slash Their Forecasts On Cloudflare Following Q3 Results. Cloudflare beat Q3 estimates, raised "
     "full-year guidance, and shares surged 19.9%; analysts cut price targets despite strong results.", 1),
    ("Telephone and Data Q3 EPS $0.26 Beats $0.13 Estimate, Sales $306.700M Miss $309.252M Estimate", 0),
    ("Shares dropped after the company lowered its outlook and disappointed investors", -1),
    ("Top picks for the week ahead", 0),
])
def test_tone_reads_word_forms(text, sign):
    from app.services.news_sentiment import score_headline_sentiment

    score = score_headline_sentiment(text)
    assert (score > 0) - (score < 0) == sign


def test_articles_are_stored_once_per_named_company(db):  # noqa: F811
    assert nd.store_articles(db, NEWS) == 11  # roundups skipped; article 3 stored for PLTR and MSFT
    assert nd.store_articles(db, NEWS) == 0
    rows = db.query(NewsArticle).filter(NewsArticle.symbol == "PLTR").all()
    assert {row.source for row in rows} == {"alpaca_news"} and len(rows) == 3
    assert min(float(row.relevance_score) for row in rows) == 0.5


def _frame(close=40.0, volume=2_000_000, days=90):
    dates = pd.bdate_range(end=NOW.date(), periods=days)
    return pd.DataFrame({"date": dates.date, "open": close, "high": close * 1.01, "low": close * 0.99,
                         "close": close, "adjusted_close": close, "volume": volume})


def _importer(prices):
    def load(db, symbol):
        upsert_prices(db, symbol, _frame(**prices.get(symbol, {})), "yfinance")
    return load


def _asset(symbol, **overrides):
    return {"symbol": symbol, "name": f"{symbol} Inc.", "class": "us_equity", "status": "active",
            "tradable": True, "exchange": "NASDAQ", **overrides}


def _screen(db, symbol, *, asset=None, prices=None, max_price=1500.0):
    return nd.screen_symbol(db, symbol, max_price=max_price,
                            lookup_asset=lambda s: asset if asset is not None else _asset(s),
                            import_prices=_importer({symbol: prices or {}}))


def test_screen_admits_a_liquid_listed_stock_a_learning_trade_can_buy(db):  # noqa: F811
    result = _screen(db, "PLTR")
    assert result["admitted"] and result["price"] == 40.0 and result["dollar_volume_20d"] == 80_000_000
    # The price import's asset row stays inactive until admission.
    assert db.query(Asset).filter_by(symbol="PLTR").one().is_active is False


@pytest.mark.parametrize("asset, prices, reason", [
    ({"exchange": "OTC"}, {}, "not a listed exchange"),
    ({"tradable": False}, {}, "not an active, tradable US equity"),
    ({}, {"close": 3.0}, "below 5.00"),
    ({}, {"close": 2000.0}, "above the one-share learning cap"),
    ({}, {"volume": 10_000}, "dollar volume"),
    ({}, {"days": 30}, "daily history unavailable"),
])
def test_screen_rejections(db, asset, prices, reason):  # noqa: F811
    result = _screen(db, "SOFI", asset=_asset("SOFI", **asset), prices=prices)
    assert result["admitted"] is False and reason in result["reason"]


def _discover(db, *, now=NOW, news=NEWS, prices=None, fail=False):  # noqa: F811
    def fetch(start, end):
        if fail:
            raise nd.NewsUnavailable("down")
        return news
    return nd.run_news_discovery(db, now=now, fetch_news=fetch, lookup_asset=_asset,
                                 import_prices=_importer(prices or {}))


def test_discovery_admits_expires_and_keeps_held_symbols_for_exits(db):  # noqa: F811
    first = _discover(db)
    assert [row["symbol"] for row in first["admitted"]] == ["PLTR", "SOFI"]
    assert first["newly_admitted"] == ["PLTR", "SOFI"] and first["managed"] == ["PLTR", "SOFI"]
    assert db.query(Asset).filter_by(symbol="PLTR").one().is_active is True
    admitted, exits_only = nd.news_universe(db, NOW)
    assert set(admitted) == {"PLTR", "SOFI"} and exits_only == set()
    assert admitted["PLTR"]["expires_at"] == (NOW + timedelta(days=3)).isoformat()

    # Still in the news a day later: the window is extended, not re-screened.
    later = _discover(db, now=NOW + timedelta(days=1), news=NEWS[:3])
    pltr = next(row for row in later["admitted"] if row["symbol"] == "PLTR")
    assert pltr["expires_at"] == (NOW + timedelta(days=4)).isoformat() and later["newly_admitted"] == []

    # SOFI's window lapses while a position is still open; PLTR's while flat.
    db.add(StockPaperPosition(account_id=1, symbol="SOFI", quantity=Decimal("1"),
                              observed_at=NOW.replace(tzinfo=None), raw_payload={}))
    db.flush()
    expired = _discover(db, now=NOW + timedelta(days=5), news=[])
    assert expired["admitted"] == [] and expired["expired"] == ["PLTR", "SOFI"]
    assert expired["holding"] == ["SOFI"] and expired["managed"] == ["SOFI"]
    assert db.query(Asset).filter_by(symbol="PLTR").one().is_active is False
    assert db.query(Asset).filter_by(symbol="SOFI").one().is_active is True
    assert nd.news_universe(db, NOW + timedelta(days=5)) == ({}, {"SOFI"})


def test_discovery_respects_the_cap_and_keeps_admissions_when_news_is_down(db, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "news_discovery_max_symbols", 1)
    first = _discover(db)
    assert [row["symbol"] for row in first["admitted"]] == ["PLTR"] and first["cap"] == 1
    down = _discover(db, now=NOW + timedelta(hours=1), fail=True)
    assert down["status"] == "news_unavailable" and [row["symbol"] for row in down["admitted"]] == ["PLTR"]
    rows = db.query(AuditLog).filter(AuditLog.event_type == nd.EVENT_TYPE).count()
    assert rows == 2


def test_rejected_candidates_are_recorded_and_left_inactive(db):  # noqa: F811
    result = _discover(db, prices={"SOFI": {"close": 2.5}})
    assert [row["symbol"] for row in result["admitted"]] == ["PLTR"]
    assert result["rejected"][0]["symbol"] == "SOFI" and "below" in result["rejected"][0]["reason"]
    assert db.query(Asset).filter_by(symbol="SOFI").one().is_active is False


def test_an_existing_active_asset_is_never_deactivated(db):  # noqa: F811
    db.add(Asset(symbol="PLTR", name="Palantir", is_active=True))
    db.flush()
    first = _discover(db)
    assert "PLTR" not in first["managed"]
    _discover(db, now=NOW + timedelta(days=5), news=[])
    assert db.query(Asset).filter_by(symbol="PLTR").one().is_active is True


def test_discovered_symbols_join_the_intraday_universe(monkeypatch):
    from app.services import trading_universe

    monkeypatch.setattr(nd, "_cache", {"at": 1e18, "symbols": frozenset({"PLTR"})})
    monkeypatch.setattr(nd.time, "monotonic", lambda: 1e18)
    assert "PLTR" in trading_universe.intraday_symbols()
    monkeypatch.setattr(settings, "news_discovery_enabled", False)
    assert "PLTR" not in trading_universe.intraday_symbols()


def test_alpaca_news_is_paged(monkeypatch):
    monkeypatch.setattr(type(settings), "research_alpaca_credentials", lambda self: ("key", "secret"))
    pages = {None: {"news": [NEWS[0]], "next_page_token": "p2"}, "p2": {"news": [NEWS[1]], "next_page_token": None}}
    seen = []

    def handler(request):
        seen.append(request.url.params)
        return httpx.Response(200, json=pages[request.url.params.get("page_token")])

    client = httpx.Client(base_url="https://data.example", transport=httpx.MockTransport(handler))
    articles = nd.fetch_alpaca_news(NOW - timedelta(hours=24), NOW, client=client)
    assert [item["id"] for item in articles] == [1, 2]
    assert seen[0]["sort"] == "desc" and seen[0]["limit"] == "50"


def test_news_job_is_scheduled_on_the_fast_learning_queue():
    from app.tasks.celery_app import celery_app

    assert celery_app.conf.beat_schedule["news-discovery"]["task"] == "app.tasks.jobs.news_discovery_job"
    assert celery_app.conf.task_routes["app.tasks.jobs.news_discovery_job"] == {"queue": "learning_fast"}


# ------------------------------------------------------------ the paper cycle

_MA = SimpleNamespace(id=1, strategy_type="moving_average_crossover")
_MACD = SimpleNamespace(id=3, strategy_type="macd_momentum")


@pytest.fixture()
def _no_news(monkeypatch):
    monkeypatch.setattr(settings, "news_discovery_enabled", False)


@pytest.mark.usefixtures("_no_news")
def test_two_strategies_buying_one_symbol_in_a_cycle_open_one_position():
    db = _ModelDb([_MA, _MACD], {})
    result, reserve = _run(db, {"moving_average_crossover": "BUY", "macd_momentum": "BUY"}, {})
    assert reserve.call_count == 1
    by_strategy = {row["strategy"]: row for row in result["results"]}
    assert by_strategy["moving_average_crossover"]["status"] == "submitted"
    assert by_strategy["macd_momentum"]["status"] == "skipped"
    assert "one position per symbol: moving_average_crossover" in by_strategy["macd_momentum"]["reason"]


@pytest.mark.usefixtures("_no_news")
def test_another_strategy_holding_or_buying_the_symbol_blocks_the_entry():
    db = _ModelDb([_MACD], {})
    held, reserve = _run(db, {"macd_momentum": "BUY"}, {"moving_average_crossover": Decimal("3")})
    reserve.assert_not_called()
    assert "moving_average_crossover already holds or is buying SPY" in held["results"][0]["reason"]
    pending = [SimpleNamespace(id=9, side="buy", strategy_id=1)]
    buying, reserve = _run(db, {"macd_momentum": "BUY"}, {}, pending=pending)
    reserve.assert_not_called()
    assert "one position per symbol" in buying["results"][0]["reason"]


@pytest.mark.usefixtures("_no_news")
def test_one_position_per_symbol_can_be_turned_off(monkeypatch):
    monkeypatch.setattr(settings, "paper_one_position_per_symbol", False)
    db = _ModelDb([_MA, _MACD], {})
    _result, reserve = _run(db, {"moving_average_crossover": "BUY", "macd_momentum": "BUY"}, {})
    assert reserve.call_count == 2


def _with_news(monkeypatch, admitted, exits_only=()):
    from app.services import stock_paper_autotrader

    monkeypatch.setattr("app.services.news_discovery.news_universe",
                        lambda db, now: (admitted, set(exits_only)))
    return stock_paper_autotrader


def test_admitted_news_symbol_is_traded_learning_sized_and_tagged(monkeypatch):
    news = {"symbol": "PLTR", "articles": 3, "avg_sentiment": 0.8, "admitted_at": NOW.isoformat(),
            "headlines": [{"headline": "Palantir soars"}]}
    autotrader = _with_news(monkeypatch, {"PLTR": news})
    tagged = []
    monkeypatch.setattr(autotrader, "_tag_signal_news", lambda db, signal_id, row: tagged.append(row["symbol"]))
    monkeypatch.setattr(settings, "paper_exploration_risk_fraction", 0.0025)
    db = _ModelDb([_MA], {})
    result, reserve = _run(db, {"moving_average_crossover": "BUY"}, {})
    assert result["symbols"] == ["SPY", "PLTR"]
    assert result["news_discovered"] == {"admitted": ["PLTR"], "exits_only": []}
    entry = next(kw for _, kw in reserve.call_args_list if kw["symbol"] == "PLTR")
    spy = next(kw for _, kw in reserve.call_args_list if kw["symbol"] == "SPY")
    assert entry["quantity"] < spy["quantity"]  # 0.25% learning size vs the 1% risk budget
    row = next(r for r in result["results"] if r["symbol"] == "PLTR")
    assert row["news_discovery"]["sentiment_score"] == 0.8 and row["news_discovery"]["top_headline"] == "Palantir soars"
    assert "allocation_weight" not in row and tagged == ["PLTR"]


def test_expired_news_symbol_stays_in_the_cycle_for_exits_only(monkeypatch):
    _with_news(monkeypatch, {}, exits_only={"SOFI"})
    db = _ModelDb([_MA], {})
    result, reserve = _run(db, {"moving_average_crossover": "BUY"}, {})
    assert result["symbols"] == ["SPY", "SOFI"]
    assert all(kw["symbol"] != "SOFI" for _, kw in reserve.call_args_list)
    row = next(r for r in result["results"] if r["symbol"] == "SOFI")
    assert row["status"] == "skipped" and "exits only" in row["reason"]


def test_news_tag_feeds_the_loss_diagnosis(db):  # noqa: F811
    from app.models import Strategy, StrategySignal
    from app.services import loss_diagnosis
    from app.services.stock_paper_autotrader import _tag_signal_news

    strategy = Strategy(name="MA", strategy_type="moving_average_crossover", parameters={})
    db.add(strategy)
    db.flush()
    signal = StrategySignal(strategy_id=strategy.id, symbol="PLTR", signal_time=NOW.replace(tzinfo=None),
                            action="BUY", features={"rsi": 40})
    db.add(signal)
    db.flush()
    _tag_signal_news(db, signal.id, {"avg_sentiment": 0.8, "articles": 3, "headlines": []})
    db.refresh(signal)
    assert signal.features["rsi"] == 40
    assert loss_diagnosis.news_from_features(signal.features) == (True, 0.8)
    result = loss_diagnosis.diagnose(gross_return=-0.03, is_loss=True, benchmark_return=0.0,
                                     news_used=True, news_sentiment=0.8)
    assert result["cause"] == "news_wrong"
