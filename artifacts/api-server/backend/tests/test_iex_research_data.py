from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import required_role
from app.db.base import Base
from app.models import AuditLog, IntradayBar
from app.services import alpaca_research_data as iex
from app.services.exchange_sessions import is_nyse_session, session_bounds
from app.services.intraday_data import feed_status

UTC = timezone.utc
START = datetime(2026, 9, 28, 13, 30, tzinfo=UTC)
END = START + timedelta(minutes=10)
NOW = END + timedelta(minutes=1)


def bar(at=START, **overrides):
    return {"t": at.isoformat(), "o": 100, "h": 102, "l": 99, "c": 101, "v": 20, **overrides}


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.mark.parametrize("day", [date(2001, 9, 11), date(2012, 10, 29), date(2018, 12, 5), date(2025, 1, 9)])
def test_exceptional_exchange_closures(day):
    assert not is_nyse_session(day)
    assert session_bounds(day) is None


def test_early_close_and_dst():
    assert session_bounds(date(2025, 7, 3))[1] == datetime(2025, 7, 3, 17, tzinfo=UTC)
    assert session_bounds(date(2026, 3, 9))[0].hour == 13
    assert session_bounds(date(2026, 3, 6))[0].hour == 14


def test_window_withholds_unfinished_and_late_correctable_minutes():
    assert iex.collection_window(NOW) == (START, END)
    friday = datetime(2026, 9, 25, 20, tzinfo=UTC)
    assert iex.collection_window(START)[1] == friday
    with pytest.raises(ValueError):
        iex.collection_window(NOW.replace(tzinfo=None))


def test_sdk_requests_iex_raw_and_follows_pages():
    client = Mock()
    client.get.side_effect = [
        {"bars": {"SPY": [bar()]}, "next_page_token": "next"},
        {"bars": {"SPY": [bar(START + timedelta(minutes=2))]}, "next_page_token": None},
    ]
    result = iex.fetch_iex_bars(["SPY"], START, END, client=client)
    assert len(result["SPY"]) == 2
    assert client.get.call_count == 2
    fields = client.get.call_args.kwargs["data"]
    assert fields["feed"] == "iex"
    assert fields["adjustment"] == "raw"
    assert str(fields["timeframe"]) == "1Min"


@pytest.mark.parametrize("bad", [
    bar(START - timedelta(minutes=1)), bar(END), bar(START + timedelta(seconds=1)),
    bar(t="2026-09-28T13:30:00"), bar(v=True), bar(c=float("nan")), bar(h=1),
])
def test_bad_bars_rejected(bad):
    client = Mock()
    client.get.return_value = {"bars": {"SPY": [bad]}}
    with pytest.raises(ValueError):
        iex.fetch_iex_bars(["SPY"], START, END, client=client)


@pytest.mark.parametrize("payload", [{}, {"bars": []}, {"bars": {"FAKE": []}}, {"bars": {"SPY": [bar(), bar()]}}])
def test_invalid_response_cannot_become_observations(payload):
    client = Mock()
    client.get.return_value = payload
    with pytest.raises(ValueError):
        iex.fetch_iex_bars(["SPY"], START, END, client=client)


def test_partial_and_repeated_pagination_rejected():
    client = Mock()
    client.get.return_value = {"bars": {}, "next_page_token": "loop"}
    with pytest.raises(ValueError, match="page token"):
        iex.fetch_iex_bars(["SPY"], START, END, client=client)
    assert client.get.call_count == 2
    client.get.side_effect = [{"bars": {}, "next_page_token": str(n)} for n in range(4)]
    with pytest.raises(ValueError, match="budget exhausted"):
        iex.fetch_iex_bars(["SPY"], START, END, client=client)


def test_replay_idempotent_sparse_feed_cannot_satisfy_sip(db):
    response = {symbol: [bar()] for symbol in iex.ALLOWED_SYMBOLS}
    with patch.object(iex, "fetch_iex_bars", return_value=response):
        for _ in range(2):
            report = iex.collect_iex_research(db, now=NOW)
            db.commit()
            assert report["execution_eligible"] is False
    assert db.query(IntradayBar).count() == 4
    assert {(r.provider, r.feed_class) for r in db.query(IntradayBar)} == {("alpaca_iex", "iex")}
    assert db.query(AuditLog).count() == 2
    assert feed_status(db, "SPY", now=NOW)["status"] != "ready"
    status = iex.iex_research_status(db, now=NOW)
    assert status["poll_fresh"]
    assert iex.iex_research_status(db, now=NOW + timedelta(seconds=120))["poll_fresh"]
    assert not iex.iex_research_status(db, now=NOW + timedelta(seconds=121))["poll_fresh"]
    assert not iex.iex_research_status(db, now=NOW - timedelta(seconds=1))["poll_fresh"]
    assert not iex.iex_research_status(db, now=NOW + timedelta(minutes=11))["poll_fresh"]
    assert status["symbols"][0]["total_bars"] == 1


def test_exception_body_is_never_persisted(db):
    with patch.object(iex, "fetch_iex_bars", side_effect=RuntimeError("secret-example")):
        report = iex.collect_iex_research(db, now=NOW)
        db.commit()
    assert report["status"] == "unavailable"
    assert "secret-example" not in str(db.query(AuditLog).one().payload)
    assert db.query(IntradayBar).count() == 0


def test_transport_bounds_and_no_redirects():
    with patch.object(type(settings), "paper_broker_credentials", return_value=("test", "test")):
        client = iex.BoundedStockDataClient()
    try:
        with patch("alpaca.common.rest.RESTClient._one_request", return_value={}) as request:
            client._one_request("GET", "https://data.alpaca.markets/v2/stocks/bars", {}, 3)
            assert request.call_args.args[2] == {"timeout": (3, 12), "allow_redirects": False}
            assert request.call_args.args[3] == 0
            with pytest.raises(RuntimeError):
                client._one_request("POST", "https://paper-api.alpaca.markets/v2/orders", {}, 0)
    finally:
        client.close()


def test_research_endpoint_roles():
    assert required_role("GET", "/market-data/iex/status") == "viewer"
    assert required_role("POST", "/market-data/iex/collect") == "researcher"
