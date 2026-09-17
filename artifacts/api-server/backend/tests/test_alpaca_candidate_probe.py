import pytest

from scripts.validate_alpaca_paper_evidence import ReadOnlyCandidate, numeric


@pytest.mark.parametrize("method,path,payload", [
    ("POST", "/v2/orders", {}),
    ("DELETE", "/v2/orders/test", None),
    ("GET", "/v2/unknown", None),
    ("GET", "/v2/account", {}),
])
def test_candidate_rejects_mutation_and_unapproved_endpoints(method, path, payload):
    with pytest.raises(RuntimeError, match="evidence GETs only"):
        ReadOnlyCandidate()._request_response(method, path, payload=payload)


@pytest.mark.parametrize("value", [None, "NaN", "Infinity", "not-a-number"])
def test_invalid_balance_is_not_numeric(value):
    assert not numeric(value)


def test_explicit_zero_is_numeric():
    assert numeric("0")