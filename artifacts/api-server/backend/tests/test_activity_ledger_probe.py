from app.core.config import settings
from scripts import validate_alpaca_activity_ledger as probe
from copy import deepcopy
import pytest


def test_probe_uses_scratch_database_and_restores_provider(monkeypatch):
    class Broker:
        def account(self):
            return {"id": "test", "status": "ACTIVE", "currency": "USD", "cash": "1000", "equity": "1000", "last_equity": "1000", "buying_power": "1000"}
        def orders(self, after=None):
            return []
        def positions(self):
            return []
        def fills(self, after=None):
            return [{"id": "cash", "activity_type": "CSD", "date": "2026-01-01", "net_amount": "1000"}]
    monkeypatch.setattr(probe, "ReadOnlyCandidate", Broker)
    monkeypatch.setattr(settings, "active_paper_broker", "tradier_sandbox")
    monkeypatch.setattr(settings, "paper_broker_account_id", "")
    monkeypatch.setattr(settings, "database_url", "postgresql://unreachable.invalid/never-connect")
    result = probe.validate()
    assert result["status"] == "passed"
    assert result["replay_unchanged"]
    assert not result["application_database_modified"]
    assert all(row["activity_count"] == 1 and row["fill_count"] == 0 for row in result["reads"])
    assert settings.active_paper_broker == "tradier_sandbox"


@pytest.mark.parametrize("field", ["activity_count", "fill_count", "event_count", "journal_sha256", "baseline_journal_sha256"])
def test_probe_rejects_changed_replay_evidence(field):
    row = {"activity_count": 1, "fill_count": 0, "reconciliation": {
        "event_count": 1, "journal_sha256": "first", "baseline_journal_sha256": "baseline"}}
    other = deepcopy(row)
    target = other if field in other else other["reconciliation"]
    target[field] = "changed"
    assert not probe.replay_matches([row, other])


def test_probe_requires_two_reads_and_a_journal_hash():
    assert not probe.replay_matches([])
    assert not probe.replay_matches([{}, {}])
