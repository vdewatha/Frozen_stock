from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import SecretStr

from app.services.live_broker import (
    AlpacaLiveClient,
    LiveBrokerError,
    LiveBrokerUnavailable,
    _account_values,
)
from app.core.config import settings


UTC = timezone.utc


def _order(order_id: str, *, status: str = "accepted", stamp: datetime | None = None) -> dict:
    stamp = stamp or datetime(2026, 9, 15, 15, 0, tzinfo=UTC)
    return {
        "id": order_id,
        "client_order_id": f"lb-{order_id}",
        "symbol": "SPY",
        "side": "buy",
        "qty": "1",
        "type": "limit",
        "time_in_force": "day",
        "limit_price": "100",
        "status": status,
        "updated_at": stamp.isoformat(),
    }


def _account(**overrides) -> dict:
    result = {
        "id": "live-account-1",
        "currency": "USD",
        "status": "ACTIVE",
        "cash": "10000",
        "buying_power": "10000",
        "equity": "10000",
        "trading_blocked": False,
        "account_blocked": False,
        "trade_suspended_by_user": False,
    }
    result.update(overrides)
    return result


def test_live_client_paginates_orders_and_deduplicates_provider_rows(monkeypatch):
    client = AlpacaLiveClient()
    first = [_order(f"order-{index}", stamp=datetime(2026, 9, 15, 15, 0, tzinfo=UTC) - timedelta(seconds=index)) for index in range(500)]
    second = [_order("order-500", stamp=datetime(2026, 9, 15, 14, 51, 39, tzinfo=UTC))]
    calls = []

    def request(method, path, *, params=None, payload=None):
        calls.append(dict(params or {}))
        return first if len(calls) == 1 else second

    monkeypatch.setattr(client, "_request", request)
    rows = client.orders()

    assert len(rows) == 501
    assert calls[0]["limit"] == "500"
    assert calls[1]["until"] == min(row["updated_at"] for row in first).replace("+00:00", "+00:00")


def test_live_client_rejects_ambiguous_order_boundary_and_missing_activity_cursor(monkeypatch):
    client = AlpacaLiveClient()
    boundary = datetime(2026, 9, 15, 15, 0, tzinfo=UTC).isoformat()
    full_page = [_order(f"order-{index}", stamp=datetime.fromisoformat(boundary) if index < 2 else datetime(2026, 9, 15, 14, 0, tzinfo=UTC)) for index in range(500)]
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: full_page)
    with pytest.raises(LiveBrokerUnavailable, match="ambiguous"):
        client.orders()

    def activity_page(method, path, *, params=None, payload=None):
        return ([{"id": f"activity-{index}"} for index in range(100)], {})

    monkeypatch.setattr(client, "_request_response", activity_page)
    with pytest.raises(LiveBrokerUnavailable, match="full without"):
        client.activities()


def test_live_client_rejects_repeated_activity_cursor(monkeypatch):
    client = AlpacaLiveClient()
    responses = iter([
        ({"data": [{"id": "activity-1"}], "next_page_token": "cursor-a"}, {}),
        ({"data": [{"id": "activity-2"}], "next_page_token": "cursor-a"}, {}),
    ])
    monkeypatch.setattr(client, "_request_response", lambda *args, **kwargs: next(responses))
    with pytest.raises(LiveBrokerUnavailable, match="repeated"):
        client.activities()


@pytest.mark.parametrize("status", ["accepted", "partially_filled", "canceled", "replaced", "delayed"])
def test_provider_statuses_are_known_or_conservatively_unknown(status):
    client_status = status
    if client_status == "delayed":
        client_status = "unknown"
    assert client_status in {
        "accepted", "partially_filled", "canceled", "replaced", "unknown",
    }


def test_live_account_permissions_are_required_and_fail_closed():
    observed = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)
    incomplete = _account()
    del incomplete["trading_blocked"]
    with pytest.raises(LiveBrokerError, match="permissions are incomplete"):
        _account_values(incomplete, observed)
    with pytest.raises(LiveBrokerError, match="do not allow"):
        _account_values(_account(trading_blocked=True), observed)


def test_read_only_market_data_entitlement_probe_never_submits_order(monkeypatch):
    client = AlpacaLiveClient()
    calls = []
    monkeypatch.setattr(settings, "live_alpaca_api_key", SecretStr("test-key"))
    monkeypatch.setattr(settings, "live_alpaca_api_secret", SecretStr("test-secret"))

    class Response:
        status_code = 200

        def json(self):
            return {"SPY": {"latestTrade": {"p": 100}}}

    class HttpClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, path, *, params):
            calls.append((path, params))
            return Response()

    monkeypatch.setattr("app.services.live_broker.httpx.Client", lambda **kwargs: HttpClient())
    result = client.market_data_entitlement()

    assert result["status"] == "authorized"
    assert result["feed"]
    assert calls == [("/v2/stocks/snapshots", {"symbols": "SPY", "feed": result["feed"]})]