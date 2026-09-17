import pytest

from scripts.validate_alpaca_paper_evidence import ReadOnlyCandidate, numeric
from scripts.validate_alpaca_paper_evidence import main


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


def test_probe_emits_redacted_contract_failure_without_qualifying(monkeypatch, capsys):
    import json

    monkeypatch.setattr(ReadOnlyCandidate, "account", lambda self: {
        "id": "private-account", "cash": "1000", "equity": "1000",
    })
    monkeypatch.setattr(ReadOnlyCandidate, "positions", lambda self: [])
    monkeypatch.setattr(ReadOnlyCandidate, "orders", lambda self: [])
    monkeypatch.setattr(ReadOnlyCandidate, "fills", lambda self: [{
        "id": "private-activity", "activity_type": "JNLC", "date": "2026-09-15",
        "net_amount": "1000", "description": "private-description",
    }])
    main()
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["qualification"] == "not_established_by_read_only_probe"
    assert report["history_replay_equal"]["fills"] is True
    for sample in report["reads"]:
        contract = sample["cost_timestamp_contract"]
        assert contract["result"] == "not_established"
        assert contract["cost_evidence"] == contract["activity_timestamp_evidence"] == "unknown"
        assert contract["launch_authorized"] is False
    assert "private-" not in output
    assert "net_amount" not in output