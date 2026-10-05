from copy import deepcopy
from datetime import datetime, timezone

import pandas as pd
import pytest

from app.services import alpaca_order_history as history
from app.services.live_broker import AlpacaLiveClient, LiveBrokerUnavailable
from app.services.stock_paper_ledger import AlpacaPaperClient, StockPaperUnavailable, RECONCILIATION_OVERLAP


@pytest.fixture(params=[(AlpacaPaperClient, StockPaperUnavailable), (AlpacaLiveClient, LiveBrokerUnavailable)])
def client(request):
    cls, error = request.param
    return cls(), error


def orders(count=1003):
    start = pd.Timestamp("2026-09-29T14:00:00.000000123Z")
    return [{"id": f"order-{i}", "submitted_at": (start + pd.Timedelta(i//3, unit="ns")).isoformat(),
             "updated_at": "2026-09-30T18:00:00Z", "created_at": "2026-09-28T18:00:00Z"}
            for i in reversed(range(count))]


def api_for(rows, calls):
    def request(method, path, *, params):
        assert method == "GET" and path == "/v2/orders"
        calls.append(params)
        eligible = rows if "until" not in params else [r for r in rows if pd.Timestamp(r["submitted_at"]) < pd.Timestamp(params["until"])]
        return deepcopy(eligible[:500])
    return request


def test_all_orders_preserved_across_nanosecond_ties_and_update_time_changes(client, monkeypatch):
    c, _ = client
    rows, calls = orders(), []
    monkeypatch.setattr(c, "_request", api_for(rows, calls))
    assert c.orders() == rows
    assert len(calls) == 3
    assert pd.Timestamp(calls[1]["until"]) == pd.Timestamp(rows[499]["submitted_at"]) + pd.Timedelta(1, unit="ns")
    assert all("before_order_id" not in call for call in calls)


def test_after_filter_remains_on_every_page(monkeypatch):
    c, calls = AlpacaPaperClient(), []
    monkeypatch.setattr(c, "_request", api_for(orders(), calls))
    after = datetime(2026, 9, 28, tzinfo=timezone.utc)
    c.orders(after)
    assert len(calls) == 3
    assert all(call["after"] == (after-RECONCILIATION_OVERLAP).isoformat() for call in calls)


@pytest.mark.parametrize("case", ["missing_timestamp", "naive", "invalid_timestamp", "missing_identity", "duplicate_identity", "unsorted", "non_list", "oversized"])
def test_invalid_pages_never_return_partial_history(client, monkeypatch, case):
    c, error = client
    page = orders(500)
    if case == "missing_timestamp":
        page[-1].pop("submitted_at")
    elif case == "naive":
        page[-1]["submitted_at"] = "2026-09-29T14:00:00"
    elif case == "invalid_timestamp":
        page[-1]["submitted_at"] = "invalid"
    elif case == "missing_identity":
        page[-1].pop("id")
    elif case == "duplicate_identity":
        page[-1] = page[0]
    elif case == "unsorted":
        page.reverse()
    elif case == "non_list":
        page = {"orders": page}
    elif case == "oversized":
        page = orders(501)
    monkeypatch.setattr(c, "_request", lambda *args, **kwargs: page)
    with pytest.raises(error):
        c.orders()


@pytest.mark.parametrize("case", ["ignored_cursor", "changed_overlap", "full_tied_boundary"])
def test_unsafe_continuations_fail_closed(client, monkeypatch, case):
    c, error = client
    rows = orders(500)
    if case == "full_tied_boundary":
        rows = [{**row, "submitted_at": rows[0]["submitted_at"]} for row in rows]
    calls = []
    def request(method, path, *, params):
        calls.append(params)
        if len(calls) == 1 or case != "changed_overlap":
            return deepcopy(rows)
        return [{**rows[-1], "updated_at": "changed"}]
    monkeypatch.setattr(c, "_request", request)
    with pytest.raises(error):
        c.orders()
    assert len(calls) == 2


def test_page_limit_never_claims_complete_history(client, monkeypatch):
    c, error = client
    monkeypatch.setattr(history, "MAX_PAGES", 2)
    monkeypatch.setattr(c, "_request", api_for(orders(1500), []))
    with pytest.raises(error, match="page limit"):
        c.orders()
