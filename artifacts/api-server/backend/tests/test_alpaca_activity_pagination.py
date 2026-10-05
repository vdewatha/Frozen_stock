"""Reject incomplete or conflicting broker history before ledger ingestion."""
from unittest.mock import patch

import pytest

from app.services.stock_paper_ledger import AlpacaPaperClient, StockPaperUnavailable


@pytest.mark.parametrize("body", [{}, {"message": "temporarily unavailable"},
    {"activities": None}, {"activities": {}}, {"data": False},
    {"activities": [], "data": [{"id": "hidden"}]}])
def test_malformed_envelopes_are_not_empty_history(body):
    client = AlpacaPaperClient()
    with patch.object(client, "_request_response", return_value=(body, {})):
        with pytest.raises(StockPaperUnavailable):
            client.fills()


@pytest.mark.parametrize("separate_pages", [False, True])
def test_conflicting_duplicate_activity_requires_review(separate_pages):
    original = {"id": "fee", "activity_type": "FEE", "net_amount": "-0.01"}
    changed = {**original, "net_amount": "-0.02"}
    pages = [([original, {"id": "other"}], {}), ([changed], {})] if separate_pages else [([original, changed], {})]
    client = AlpacaPaperClient()
    with patch.object(client, "_request_response", side_effect=pages):
        with pytest.raises(StockPaperUnavailable, match="conflicting"):
            client._activity_pages({"page_size": "2" if separate_pages else "100"})


@pytest.mark.parametrize("body", [[], {"activities": []}, {"data": []}])
def test_explicit_empty_history_is_supported(body):
    client = AlpacaPaperClient()
    with patch.object(client, "_request_response", return_value=(body, {})):
        assert client.fills() == []


def test_identical_boundary_overlap_is_deduplicated():
    client = AlpacaPaperClient()
    a, b, c = {"id": "a"}, {"id": "b"}, {"id": "c"}
    with patch.object(client, "_request_response", side_effect=[([a, b], {}), ([b, c], {}), ([], {})]) as request:
        assert client._activity_pages({"page_size": "2"}) == [a, b, c]
    assert [call.kwargs["params"].get("page_token") for call in request.call_args_list] == [None, "b", "c"]
