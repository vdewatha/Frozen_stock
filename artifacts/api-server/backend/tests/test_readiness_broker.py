from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services import stock_paper_ledger as ledger
from app.services.broker import broker_status, stock_paper_broker_status


@pytest.mark.parametrize("venue", ["tradier_sandbox", "alpaca_paper"])
@pytest.mark.parametrize("initialized", [False, True])
def test_active_venue_and_accounting_are_separate(monkeypatch, venue, initialized):
    monkeypatch.setattr(ledger.settings, "active_paper_broker", venue)
    account = SimpleNamespace(
        status="reconciled", accounting_verified=True, costs_known=True,
        reconciliation_required=False, unexplained_residual=False,
    ) if initialized else None
    db = Mock()
    db.query.return_value.filter_by.return_value.one_or_none.return_value = account
    result = stock_paper_broker_status(db)
    db.query.return_value.filter_by.assert_called_once_with(broker=venue, archived_at=None)
    assert result["paper_broker"] == venue
    assert result["live_trading_blocked"] is True
    assert result["paper_trading_enabled"] is True
    assert result["accounting"]["ready"] is (initialized and venue == "alpaca_paper")
    assert "separate required gate" in result["message"]
    db.commit.assert_not_called()
    db.add.assert_not_called()
    assert "account_id" not in str(result)


@pytest.mark.parametrize("field,value", [
    ("status", "halted"), ("accounting_verified", False), ("costs_known", False),
    ("reconciliation_required", True), ("unexplained_residual", True),
])
def test_incomplete_accounting_fails_closed(monkeypatch, field, value):
    monkeypatch.setattr(ledger.settings, "active_paper_broker", "alpaca_paper")
    account = SimpleNamespace(
        status="reconciled", accounting_verified=True, costs_known=True,
        reconciliation_required=False, unexplained_residual=False,
    )
    setattr(account, field, value)
    monkeypatch.setattr(ledger, "active_paper_account", lambda db: account)
    assert stock_paper_broker_status(Mock())["accounting"]["ready"] is False


def test_legacy_order_contract_remains_separate():
    assert broker_status()["paper_broker"] == "internal_paper_stub"


def test_unsupported_venue_is_not_reported_safe(monkeypatch):
    monkeypatch.setattr(ledger.settings, "active_paper_broker", "unsupported")
    with pytest.raises(ledger.StockPaperError):
        stock_paper_broker_status(Mock())
