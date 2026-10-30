"""Account hand-off between deployments that previously shared one paper account."""
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401  register all tables
from app.core.config import Settings, settings
from app.core.security import required_role
from app.db.base import Base
from app.models.stock_paper import (
    StockPaperAccount, StockPaperFill, StockPaperLedgerEvent, StockPaperOrder, StockPaperPosition,
)
from app.services.stock_paper_ledger import (
    StockPaperError, initialize_stock_paper_account, stock_paper_status, transition_stock_paper_account,
)
from tests.test_stock_paper_ledger import FakeAlpaca

GATEWAY = "app.services.stock_paper_ledger.active_paper_gateway"


def account_row(account_id, cash="5000"):
    return {"id": account_id, "currency": "USD", "status": "ACTIVE", "cash": cash,
            "buying_power": cash, "equity": cash, "last_equity": cash,
            "updated_at": "2026-01-01T15:00:00Z"}


def shared_book():
    """A book placed by another deployment and only imported here."""
    return FakeAlpaca(
        account=account_row("shared-account", cash="900"),
        positions=[{"symbol": "SPY", "qty": "1", "market_value": "100"}],
        orders=[
            {"id": "parent-1", "client_order_id": "sp-other-deployment-1", "symbol": "SPY", "side": "buy",
             "qty": "1", "type": "limit", "limit_price": "100", "time_in_force": "day", "status": "filled",
             "order_class": "bracket"},
            {"id": "stop-1", "client_order_id": "sp-other-deployment-1-stop", "symbol": "SPY", "side": "sell",
             "qty": "1", "type": "stop", "stop_price": "95", "time_in_force": "gtc", "status": "held"},
        ],
        activities=[{"id": "fill-1", "activity_type": "FILL", "order_id": "parent-1", "symbol": "SPY",
                     "side": "buy", "qty": "1", "price": "100", "transaction_time": "2026-01-01T14:45:00Z"}],
    )


class AccountTransitionTests(unittest.TestCase):
    def setUp(self):
        self.patches = [
            patch.object(settings, "active_paper_broker", "alpaca_paper"),
            patch.object(settings, "paper_broker_account_id", ""),
        ]
        for item in self.patches:
            item.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_engine("sqlite:///" + str(Path(self.tmp.name) / "transition.sqlite"))
        Base.metadata.create_all(self.engine)
        with Session(self.engine) as db:
            result = initialize_stock_paper_account(db, shared_book())
            self.assertEqual(result["status"], "halted")
            self.assertIn("Externally submitted", result["account"]["halt_reason"])

    def tearDown(self):
        self.engine.dispose()
        self.tmp.cleanup()
        for item in reversed(self.patches):
            item.stop()

    def new_gateway(self, account_id="dev-account"):
        return FakeAlpaca(account=account_row(account_id))

    def test_all_imported_book_is_handed_off_and_new_account_uses_v2(self):
        with Session(self.engine) as db, patch(GATEWAY, return_value=self.new_gateway()), \
                patch.object(settings, "paper_broker_account_id", "dev-account"):
            result = transition_stock_paper_account(db, reason="Production owns the shared paper account")
            self.assertEqual(result["status"], "reconciled")
            self.assertEqual(result["account"]["cash"], "5000.00000000")
            self.assertEqual(result["positions"], [])

            old, new = db.query(StockPaperAccount).order_by(StockPaperAccount.id).all()
            # The old ledger is archived, not reset: halt and history stay intact.
            self.assertIsNotNone(old.archived_at)
            self.assertEqual(old.archive_reason, "Production owns the shared paper account")
            self.assertEqual(old.status, "halted")
            self.assertIn("Externally submitted", old.halt_reason)
            self.assertEqual(db.query(StockPaperPosition).filter_by(account_id=old.id).count(), 1)
            self.assertEqual(db.query(StockPaperOrder).filter_by(account_id=old.id).count(), 2)
            self.assertEqual(db.query(StockPaperFill).filter_by(account_id=old.id).count(), 1)

            self.assertIsNone(new.archived_at)
            self.assertEqual(new.broker_account_id, "dev-account")
            self.assertEqual(new.activity_contract, "alpaca-activities-v2")
            self.assertIsNotNone(new.activity_baseline)
            self.assertIsNone(new.halt_reason)
            self.assertFalse(new.costs_known)
            self.assertFalse(new.accounting_verified)

            event = db.query(StockPaperLedgerEvent).filter_by(
                account_id=old.id, event_type="account_transition").one()
            self.assertEqual(event.status, "archived")
            self.assertTrue(event.payload["handed_off_to_other_deployment"])
            self.assertEqual(event.payload["imported_open_order_count"], 1)
            self.assertEqual(event.payload["imported_open_position_count"], 1)
            self.assertEqual(event.payload["imported_open_position_symbols"], ["SPY"])
            self.assertNotIn("shared-account", str(event.payload))
            self.assertEqual(stock_paper_status(db)["account"]["cash"], "5000.00000000")

    def test_locally_reserved_nonterminal_order_refuses(self):
        with Session(self.engine) as db:
            old = db.query(StockPaperAccount).one()
            db.add(StockPaperOrder(account_id=old.id, client_order_id="sp-local-1", symbol="QQQ", side="buy",
                                   quantity=Decimal("1"), order_type="limit", time_in_force="day",
                                   limit_price=Decimal("100"), reserved_cash=Decimal("100"),
                                   status="reserved", source="manual_control_room"))
            db.commit()
            self.assert_refused(db, "nonterminal order was reserved by this local ledger")

    def test_open_position_from_locally_reserved_order_refuses(self):
        with Session(self.engine) as db:
            old = db.query(StockPaperAccount).one()
            db.add(StockPaperOrder(account_id=old.id, client_order_id="sp-local-2", symbol="SPY", side="buy",
                                   quantity=Decimal("1"), order_type="limit", time_in_force="day",
                                   limit_price=Decimal("100"), reserved_cash=Decimal("0"),
                                   status="filled", source="scheduled_paper_signal"))
            db.commit()
            self.assert_refused(db, "open position traces to a locally reserved order")

    def test_open_position_with_unlinked_fill_refuses(self):
        with Session(self.engine) as db:
            old = db.query(StockPaperAccount).one()
            db.add(StockPaperFill(account_id=old.id, order_id=None, broker_activity_id="orphan-1",
                                  symbol="SPY", side="buy", quantity=Decimal("1"), price=Decimal("100"),
                                  filled_at=datetime(2026, 1, 1, 14, tzinfo=timezone.utc), raw_payload={}))
            db.commit()
            self.assert_refused(db, "fills without an order link")

    def test_configured_binding_mismatch_refuses(self):
        with Session(self.engine) as db, patch(GATEWAY, return_value=self.new_gateway("unexpected")), \
                patch.object(settings, "paper_broker_account_id", "dev-account"):
            with self.assertRaisesRegex(StockPaperError, "configured account binding"):
                transition_stock_paper_account(db, reason="Move to development account")
            db.rollback()
            self.assertEqual(db.query(StockPaperAccount).filter_by(archived_at=None).count(), 1)

    def test_same_account_refuses(self):
        with Session(self.engine) as db, patch(GATEWAY, return_value=shared_book()):
            with self.assertRaisesRegex(StockPaperError, "already the active local account"):
                transition_stock_paper_account(db, reason="No-op")

    def test_transition_route_requires_admin(self):
        self.assertEqual(required_role("POST", "/stock-paper/account-transition"), "admin")

    def assert_refused(self, db, message):
        gateway = self.new_gateway()
        with patch(GATEWAY, return_value=gateway):
            with self.assertRaisesRegex(StockPaperError, message):
                transition_stock_paper_account(db, reason="Hand off")
        db.rollback()
        self.assertEqual(db.query(StockPaperAccount).filter_by(archived_at=None).count(), 1)
        self.assertEqual(db.query(StockPaperLedgerEvent).filter_by(event_type="account_transition").count(), 0)


class DevelopmentPaperBindingTests(unittest.TestCase):
    DEV = {"dev_paper_alpaca_api_key": "dev-key", "dev_paper_alpaca_api_secret": "dev-secret",
           "dev_paper_broker_account_id": "dev-account"}

    def test_local_environment_uses_development_binding(self):
        config = Settings(environment="local", paper_alpaca_api_key="shared-key",
                          paper_alpaca_api_secret="shared-secret", paper_broker_account_id="shared", **self.DEV)
        self.assertEqual(config.paper_broker_credentials(), ("dev-key", "dev-secret"))
        self.assertEqual(config.paper_account_binding, "dev-account")
        self.assertEqual(config.paper_binding_source, "development")

    def test_production_ignores_development_binding(self):
        config = Settings(environment="production", paper_alpaca_api_key="shared-key",
                          paper_alpaca_api_secret="shared-secret", paper_broker_account_id="shared", **self.DEV)
        self.assertEqual(config.paper_broker_credentials(), ("shared-key", "shared-secret"))
        self.assertEqual(config.paper_account_binding, "shared")
        self.assertEqual(config.paper_binding_source, "shared")

    def test_partial_development_binding_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "together"):
            # Explicit blanks keep workspace secrets from completing the set.
            Settings(environment="local", dev_paper_alpaca_api_key="dev-key",
                     dev_paper_alpaca_api_secret="", dev_paper_broker_account_id="")


if __name__ == "__main__":
    unittest.main()
