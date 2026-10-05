import httpx
import pytest

from app.services.stock_paper_ledger import AlpacaPaperClient, StockPaperUnavailable


@pytest.fixture
def transport(monkeypatch):
    client_class = httpx.Client
    requests = []

    def configure(handler):
        def respond(request):
            assert request.url.host == "paper-api.alpaca.markets"
            requests.append(request)
            return handler(request)

        monkeypatch.setattr(httpx, "Client", lambda **kwargs: client_class(
            **kwargs, transport=httpx.MockTransport(respond),
        ))
        monkeypatch.setattr(AlpacaPaperClient, "_headers", lambda self: {})
        return AlpacaPaperClient(), requests

    return configure


def test_cancel_accepts_documented_empty_204_response(transport):
    client, requests = transport(lambda request: httpx.Response(204))
    assert client.cancel_order("test-order") is None
    assert len(requests) == 1 and requests[0].method == "DELETE"


def test_lookup_absence_is_only_a_404(transport):
    client, requests = transport(lambda request: httpx.Response(404, json={"message": "not found"}))
    assert client.order_by_client_id("test-client") is None
    assert len(requests) == 1


@pytest.mark.parametrize("status", [401, 403, 422, 429, 500, 503])
def test_lookup_http_errors_preserve_uncertainty_and_redact_bodies(transport, status):
    client, requests = transport(lambda request: httpx.Response(status, json={"message": "private-broker-body"}))
    with pytest.raises(StockPaperUnavailable) as error:
        client.order_by_client_id("test-client")
    assert error.value.status_code == status
    assert "private-broker-body" not in str(error.value)
    assert len(requests) == 1


def test_timeout_is_not_treated_as_an_absent_order(transport):
    def timeout(request):
        raise httpx.ReadTimeout("private-timeout-details", request=request)
    client, requests = transport(timeout)
    with pytest.raises(StockPaperUnavailable) as error:
        client.order_by_client_id("test-client")
    assert "private-timeout-details" not in str(error.value)
    assert len(requests) == 1


@pytest.mark.parametrize("payload", [[], None, "invalid"])
def test_invalid_success_payload_is_not_order_absence(transport, payload):
    client, _ = transport(lambda request: httpx.Response(200, json=payload))
    with pytest.raises(StockPaperUnavailable):
        client.order_by_client_id("test-client")


def test_successful_lookup_preserves_the_broker_order(transport):
    client, _ = transport(lambda request: httpx.Response(200, json={"id": "test-order", "status": "filled"}))
    assert client.order_by_client_id("test-client")["status"] == "filled"


def test_cancel_rejection_is_not_swallowed(transport):
    client, _ = transport(lambda request: httpx.Response(422, json={}))
    with pytest.raises(StockPaperUnavailable):
        client.cancel_order("test-order")
