"""Learning scoreboard: deploy health checks, progress and the news split."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.core.security import required_role
from app.models import IntradayBar
from app.services import learning_scoreboard as board
from app.services import trade_attribution
from app.services.audit import write_audit_log
from tests.test_loss_feedback_loop import _book, _strategy, db  # noqa: F401  (fixture)
from tests.test_paper_trade_scorecard import Ledger, at

UTC = timezone.utc
NOW = at(19)  # Thursday 2026-10-29, 15:00 New York: session in progress
MARKET_DATA_CHECKS = ("real_macro", "event_calendar", "market_gauges")


@pytest.fixture(autouse=True)
def _alpaca_account(monkeypatch):
    monkeypatch.setattr("app.services.stock_paper_ledger.active_paper_broker_name", lambda: "alpaca_paper")


def _ledger(db, account):
    ledger = Ledger(db, account)
    ledger.counter = 100  # after the orders _book created
    return ledger


def _checks(result):
    return {item["key"]: item for item in result["health"]["checks"]}


def _news_run(db, symbols, when=at(13, 10)):
    row = write_audit_log(db, event_type="news_symbol_discovery", action="run_news_discovery", status="complete",
                          message="test", payload={
                              "observed_at": when.isoformat(), "status": "complete", "articles_fetched": 300,
                              "admitted": [{"symbol": symbol, "articles": 3, "avg_sentiment": 1.0, "price": 100,
                                            "headlines": [{"headline": f"{symbol} beats"}],
                                            "admitted_at": when.isoformat(), "expires_at": at(13, 10, day=31).isoformat()}
                                           for symbol in symbols],
                              "holding": [], "rejected": [{"symbol": "PENY", "reason": "price 2.00 is below 5.00"}]})
    row.created_at = when.replace(tzinfo=None)
    db.flush()


def _bar(db, symbol, when):
    db.add(IntradayBar(symbol=symbol, timeframe="1m", opened_at=when, open=100, high=100, low=100, close=100,
                       volume=1000, exchange_timestamp=when))
    db.flush()


def test_an_empty_account_waits_instead_of_failing(db):  # noqa: F811
    result = board.build_learning_scoreboard(db, now=NOW)
    statuses = {key: item["status"] for key, item in _checks(result).items()}
    # Placeholder economic data is a real gap (no FRED key), not a wait.
    assert statuses.pop("real_macro") == "warning"
    assert set(statuses.values()) == {"waiting"}
    assert result["health"]["session"]["date"] == "2026-10-29" and result["health"]["session"]["in_progress"]
    assert result["progress"]["overall"]["trades"] == 0 and len(result["progress"]["weeks"]) == board.WEEKS
    assert result["news"] is None and result["repeating_failures"] == []


def test_a_working_session_passes_every_check(db):  # noqa: F811
    account, _trend, _entry, _fill = _book(db)  # AAPL round trip, stopped out at 18:00
    trade_attribution.attribute_round_trips(db, account_id=account.id, earnings_lookup=None)
    _news_run(db, ["PLTR"], when=at(18, 40))
    _bar(db, "PLTR", at(18, 59))
    macd = _strategy(db, "macd_momentum")
    entry = _ledger(db, account).order(symbol="PLTR", side="buy", quantity=1, strategy=macd, order_class="bracket",
                                      stop="97.00", take="106.00", created=at(15),
                                      source="paper_exploration")
    entry.limit_price = Decimal("100")
    db.flush()
    result = board.build_learning_scoreboard(db, now=NOW)
    checks = _checks(result)
    for key in MARKET_DATA_CHECKS:
        checks.pop(key)
    assert {key: item["status"] for key, item in checks.items()} == {
        "explained": "ok", "diagnosed": "ok", "news_ran": "ok", "news_fed": "ok", "news_sized": "ok",
        "tested_exits": "ok", "one_per_symbol": "ok"}
    assert "1 of 1 new entries" in checks["tested_exits"]["detail"]
    assert result["news"]["admitted"][0]["top_headline"] == "PLTR beats"
    assert result["news"]["rejected"][0]["symbol"] == "PENY"
    # The AAPL loss is a core-universe trade, and it shows up in the week's progress.
    assert result["news_vs_core"]["core"]["trades"] == 1 and result["news_vs_core"]["news"]["trades"] == 0
    week = result["progress"]["weeks"][-1]
    assert week["week"] == "2026-W44" and week["trades"] == 1 and week["win_rate"] == 0.0
    assert result["progress"]["strategies"][0]["strategy_type"] == "trend_a"
    assert result["causes"]["diagnosed"] == 1


def test_problems_are_named(db):  # noqa: F811
    account, *_ = _book(db)  # not attributed: one closed trade unexplained
    _news_run(db, ["PLTR", "SOFI"])
    _bar(db, "PLTR", at(18, 59))
    macd, ma = _strategy(db, "macd_momentum"), _strategy(db, "moving_average_crossover")
    ledger = _ledger(db, account)
    for strategy, quantity, take in [(macd, 3, "103.00"), (ma, 1, "106.00")]:
        row = ledger.order(symbol="PLTR", side="buy", quantity=quantity, strategy=strategy, order_class="bracket",
                           stop="97.00", take=take, created=at(15))
        row.limit_price = Decimal("100")
    db.flush()
    checks = _checks(board.build_learning_scoreboard(db, now=NOW))
    assert checks["explained"]["status"] == "warning" and "1 of 1" in checks["explained"]["detail"]
    assert checks["news_fed"]["status"] == "warning" and checks["news_fed"]["evidence"]["unfed"] == ["SOFI"]
    # $300 of PLTR on $10k equity is over the ~1.5% learning size.
    assert checks["news_sized"]["status"] == "failing" and "PLTR 3" in checks["news_sized"]["detail"]
    assert checks["tested_exits"]["status"] == "failing"
    assert checks["tested_exits"]["evidence"]["untested"] == ["PLTR (macd_momentum)"]
    assert checks["one_per_symbol"]["status"] == "failing" and checks["one_per_symbol"]["evidence"]["stacked"] == ["PLTR"]


def test_stale_or_broken_news_discovery_fails_during_the_session(db):  # noqa: F811
    checks = _checks(board.build_learning_scoreboard(db, now=NOW))
    assert checks["news_ran"]["status"] == "waiting"
    _news_run(db, [], when=at(13, 10, day=28))
    assert _checks(board.build_learning_scoreboard(db, now=NOW))["news_ran"]["status"] == "failing"
    write_audit_log(db, event_type="news_symbol_discovery", action="run_news_discovery", status="news_unavailable",
                    message="down", payload={"status": "news_unavailable", "error": "NewsUnavailable: HTTP 403",
                                             "admitted": []}).created_at = at(18, 40).replace(tzinfo=None)
    db.flush()
    check = _checks(board.build_learning_scoreboard(db, now=NOW))["news_ran"]
    assert check["status"] == "failing" and "HTTP 403" in check["detail"]


def test_latest_session_skips_weekends_and_the_premarket():
    friday = board.latest_session(datetime(2026, 10, 31, 15, tzinfo=UTC))  # Saturday
    assert friday[0].isoformat() == "2026-10-30" and friday[3] is False
    premarket = board.latest_session(datetime(2026, 10, 29, 12, tzinfo=UTC))  # Thursday 8:00 ET
    assert premarket[0].isoformat() == "2026-10-28"


def test_changes_split_results_before_and_after(db):  # noqa: F811
    account, *_ = _book(db)
    trade_attribution.attribute_round_trips(db, account_id=account.id, earnings_lookup=None)
    changes = board.build_learning_scoreboard(db, now=NOW)["changes"]
    assert [change["key"] for change in changes] == ["loss_learning", "news_symbols", "market_context"]
    assert changes[0]["before"]["trades"] == 1 and changes[0]["after"]["trades"] == 0


def test_viewers_can_read_the_scoreboard():
    assert required_role("GET", "/stock-paper/learning-scoreboard") == "viewer"


def test_market_data_checks_report_real_data_calendar_and_gauges(db, monkeypatch):  # noqa: F811
    from pydantic import SecretStr

    from app.core.config import settings
    from app.models import EconomicIndicator
    from app.services import market_events

    monkeypatch.setattr(settings, "fred_api_key", SecretStr("test-key"))
    day = NOW.date()
    for name, value, source in (("treasury_10y", 4.1, "fred"), ("vix", 18.2, "yfinance"),
                                ("breadth_above_200d", 61.0, "yfinance")):
        db.add(EconomicIndicator(indicator_name=name, observation_date=day, value=Decimal(str(value)), source=source))
    db.flush()
    market_events.refresh_market_events(
        db, now=NOW, fomc=lambda: [datetime(2026, 12, 9).date()],
        fred=lambda path, params: {"release_dates": [{"date": "2026-11-13" if params["release_id"] == 10
                                                      else "2026-11-06"}]})
    write_audit_log(db, event_type="stock_paper", action=board.BUY_OUTCOMES, status="complete", message="t",
                    payload={"outcomes": [{"symbol": "AAPL", "strategy": "rsi2", "status": "skipped",
                                           "reason": "jobs report on 2026-11-06 at 08:30 ET; an overnight "
                                                     "position would be held through it"}]})
    checks = _checks(board.build_learning_scoreboard(db, now=NOW))
    assert checks["real_macro"]["status"] == "ok" and "4.10%" in checks["real_macro"]["detail"]
    assert checks["event_calendar"]["status"] == "ok", checks["event_calendar"]["detail"]
    assert "Next: jobs report on 2026-11-06" in checks["event_calendar"]["detail"]
    assert "Skipped 1 BUYs" in checks["event_calendar"]["detail"]
    assert checks["market_gauges"]["status"] == "ok" and "VIX 18.2" in checks["market_gauges"]["detail"]


def test_a_partial_calendar_says_what_is_missing(db):  # noqa: F811
    from app.services import market_events

    market_events.refresh_market_events(db, now=NOW, fomc=lambda: [datetime(2026, 12, 9).date()])
    check = _checks(board.build_learning_scoreboard(db, now=NOW))["event_calendar"]
    assert check["status"] == "warning" and "needs FRED_API_KEY" in check["detail"]


def test_a_canceled_unfilled_buy_is_not_a_second_holder(db):  # noqa: F811
    # 2026-11-06: one strategy's ABBV buy was canceled unfilled, then Model
    # Predictive Long bought ABBV; only one strategy ever held it.
    account, *_ = _book(db)
    macd, mpl = _strategy(db, "macd_momentum"), _strategy(db, "model_predictive_long")
    ledger = _ledger(db, account)
    canceled = ledger.order(symbol="ABBV", side="buy", quantity=3, strategy=macd, order_class="bracket",
                            stop="97.00", take="106.00", created=at(15))
    canceled.status = "canceled"
    ledger.order(symbol="ABBV", side="buy", quantity=3, strategy=mpl, order_class="bracket",
                 stop="97.00", take="106.00", created=at(16))
    db.flush()
    checks = _checks(board.build_learning_scoreboard(db, now=NOW))
    assert checks["one_per_symbol"]["status"] == "ok", checks["one_per_symbol"]
