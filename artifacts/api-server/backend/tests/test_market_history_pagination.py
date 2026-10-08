"""Bounded pagination and provider selection regressions, without network calls."""
from datetime import date
import json
from unittest.mock import patch

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.services import market_data


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def page(day=2, token=None):
    return {"bars": {"SPY": [{
        "t": f"2025-01-{day:02d}T00:00:00Z",
        "o": 100, "h": 102, "l": 99, "c": 101, "v": 10,
    }]}, "next_page_token": token}


@pytest.mark.parametrize("case", [
    "repeat_token", "page_bound", "invalid_token", "missing_symbol",
    "invalid_item", "request_failure", "duplicate_date",
])
def test_incomplete_or_invalid_pagination_returns_no_history(case):
    first = page(token="next")
    second = page(3)
    pages = [Response(first), Response(second)]
    if case == "repeat_token":
        second["next_page_token"] = "next"
    elif case == "page_bound":
        pages = [Response(page(i + 2, token=f"page-{i}")) for i in range(10)]
    elif case == "invalid_token":
        second["next_page_token"] = 42
    elif case == "missing_symbol":
        second["bars"] = {"AAPL": []}
    elif case == "invalid_item":
        second["bars"]["SPY"].append("not a bar")
    elif case == "request_failure":
        pages[1] = TimeoutError("fixture timeout")
    elif case == "duplicate_date":
        second["bars"] = first["bars"]
    with (
        patch.object(type(market_data.settings), "research_alpaca_credentials",
                     return_value=("test-key", "test-secret")),
        patch.object(market_data, "urlopen", side_effect=pages) as fetch,
    ):
        assert market_data.fetch_alpaca_iex_daily_prices("SPY").empty
        assert fetch.call_count <= 10


def test_pagination_carries_token_and_keeps_fixed_request_window():
    from urllib.parse import parse_qs, urlsplit

    with (
        patch.object(type(market_data.settings), "research_alpaca_credentials",
                     return_value=("test-key", "test-secret")),
        patch.object(market_data, "urlopen", side_effect=[
            Response(page(token="next")), Response(page(3)),
        ]) as fetch,
    ):
        frame = market_data.fetch_alpaca_iex_daily_prices("SPY")
    queries = [parse_qs(urlsplit(call.args[0].full_url).query)
               for call in fetch.call_args_list]
    assert "page_token" not in queries[0]
    assert queries[1].pop("page_token") == ["next"]
    assert queries[0] == queries[1]
    assert queries[0]["feed"] == ["iex"]
    assert frame.date.tolist() == [date(2025, 1, 2), date(2025, 1, 3)]


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def prices(days):
    return pd.DataFrame([{
        "date": day, "open": 100, "high": 102, "low": 99,
        "close": 101, "adjusted_close": 101, "volume": 100,
    } for day in days])


@pytest.mark.parametrize("case,expected", [
    ("freshest", "yfinance"),
    ("coverage", "alpaca_iex_daily"),
    ("deterministic_tie", "yfinance"),
])
def test_default_reader_returns_only_one_ranked_provider(db, case, expected):
    old, new = date(2025, 1, 2), date(2025, 1, 3)
    alpaca_days = [old] if case == "freshest" else [old, new]
    yahoo_days = [new] if case == "coverage" else [old, new]
    market_data.upsert_prices(db, "SPY", prices(alpaca_days), "alpaca_iex_daily")
    market_data.upsert_prices(db, "SPY", prices(yahoo_days), "yfinance")
    frame, source = market_data.get_price_history(db, "SPY", auto_seed=False)
    assert source == f"database:{expected}"
    assert set(frame.source) == {expected}
    assert not frame.date.duplicated().any()
    # Explicit selection always overrides the default ranking.
    frame, source = market_data.get_price_history(
        db, "SPY", auto_seed=False, source_filter="alpaca_iex_daily",
    )
    assert source == "database:alpaca_iex_daily"
    assert set(frame.source) == {"alpaca_iex_daily"}


def test_first_read_after_auto_seed_is_provider_bound(db):
    def seed(*_):
        frame = prices([date(2025, 1, 2)])
        market_data.upsert_prices(db, "SPY", frame, "yfinance")
        market_data.upsert_prices(db, "SPY", frame, "alpaca_iex_daily")
        return {"source": "alpaca_iex_daily"}

    with patch.object(market_data, "import_market_prices", side_effect=seed):
        frame, source = market_data.get_price_history(db, "SPY")
    assert set(frame.source) == {"alpaca_iex_daily"}
    assert source == "database:alpaca_iex_daily"
