import unittest
from unittest.mock import patch

from pydantic import SecretStr

from app.core.config import settings
from app.services.stock_paper_ledger import (
    StockPaperUnavailable,
    TradierPaperClient,
    _snapshot,
    active_paper_broker_name,
)


class TradierPaperClientTests(unittest.TestCase):
    def client(self, **overrides):
        values = {
            "tradier_api_key": SecretStr("sandbox-token"),
            "tradier_account_id": "VA000000",
            "tradier_sandbox_url": "https://sandbox.tradier.com/v1",
        }
        values.update(overrides)
        stack = patch.multiple(settings, create=True, **values)
        stack.start()
        self.addCleanup(stack.stop)
        return TradierPaperClient()

    def test_sandbox_url_and_bearer_auth_are_enforced(self):
        client = self.client()
        self.assertEqual(client.base_url, "https://sandbox.tradier.com/v1")
        self.assertEqual(client.account_id, "VA000000")
        self.assertEqual(client._headers()["Authorization"], "Bearer sandbox-token")
        with self.assertRaises(StockPaperUnavailable):
            self.client(tradier_sandbox_url="https://api.tradier.com/v1")

    def test_active_broker_uses_explicit_tradier_setting(self):
        with patch.object(settings, "active_paper_broker", "tradier_sandbox"):
            self.assertEqual(active_paper_broker_name(), "tradier_sandbox")

    def test_account_id_is_required_and_balances_are_normalized(self):
        with self.assertRaises(StockPaperUnavailable):
            self.client(tradier_account_id="")
        client = self.client()
        with patch.object(client, "_request", return_value={
            "balances": {
                "account_number": "VA000000",
                "total_equity": "1000",
                "cash": {"cash_available": "900"},
                "margin": {"stock_buying_power": "1800"},
            }
        }):
            account = client.account()
        self.assertEqual(account["id"], "VA000000")
        self.assertEqual(account["cash"], "900")
        self.assertEqual(account["buying_power"], "1800")
        self.assertEqual(account["equity"], "1000")

        with patch.object(client, "_request", return_value={
            "balances": {"account_number": "OTHER", "total_equity": "1"}
        }):
            with self.assertRaises(StockPaperUnavailable):
                client.account()

    def test_positions_and_orders_are_normalized(self):
        client = self.client()
        with patch.object(client, "_request", return_value={
            "positions": {"position": {
                "symbol": "spy", "quantity": "2", "cost_basis": "200"
            }}
        }):
            positions = client.positions()
        self.assertEqual(positions[0]["symbol"], "SPY")
        self.assertEqual(positions[0]["qty"], "2")
        self.assertEqual(positions[0]["avg_entry_price"], "100")

        raw = {"id": 12, "tag": "sp-client", "symbol": "SPY", "side": "buy",
               "quantity": 1, "type": "limit", "duration": "day",
               "price": 100, "status": "open"}
        with patch.object(client, "_request", return_value={"orders": {"order": raw}}):
            rows = client.orders()
        self.assertEqual(rows[0]["id"], "12")
        self.assertEqual(rows[0]["client_order_id"], "sp-client")
        self.assertEqual(rows[0]["limit_price"], 100)
        with patch.object(client, "_request", return_value={"order": {
            **raw, "status": "filled",
            "exec": {"id": "exec-1", "quantity": 1, "price": 99.5,
                     "execution_date": "2026-01-02T15:00:00Z"},
        }}):
            detail = client.order_details("12")
        self.assertEqual(detail["executions"][0]["id"], "exec-1")

    def test_submit_uses_form_encoding_and_lookup_is_read_only(self):
        client = self.client()
        response = {"order": {"id": 44, "status": "ok"}}
        with patch.object(client, "_request", return_value=response) as request:
            result = client.submit_order({
                "symbol": "SPY", "qty": "1", "side": "buy", "type": "limit",
                "time_in_force": "day", "limit_price": "100",
                "client_order_id": "sp-client",
            })
        request.assert_called_once_with(
            "POST", "/accounts/VA000000/orders",
            form={
                "symbol": "SPY", "quantity": "1", "side": "buy", "type": "limit",
                "duration": "day", "price": "100", "tag": "sp-client",
            },
        )
        self.assertEqual(result["id"], "44")
        self.assertEqual(result["status"], "accepted")

        with patch.object(client, "orders", return_value=[
            {"id": "44", "client_order_id": "sp-client"}
        ]):
            self.assertEqual(client.order_by_client_id("sp-client")["id"], "44")

    def test_incomplete_tradier_history_halts_snapshot(self):
        client = self.client()
        with patch.object(client, "account", return_value={"id": "VA000000"}), \
             patch.object(client, "positions", return_value=[]), \
             patch.object(client, "orders", return_value=[]), \
             patch.object(client, "fills", return_value=[]):
            with self.assertRaisesRegex(StockPaperUnavailable, "current-session-only"):
                _snapshot(client)


if __name__ == "__main__":
    unittest.main()