import unittest
from unittest.mock import MagicMock, patch

from app.services import broker


class BrokerSafetyTests(unittest.TestCase):
    def test_configuration_flag_cannot_claim_live_authorization(self):
        with patch.object(broker.settings, "allow_live_trading", True), patch.object(broker, "write_audit_log"):
            status = broker.broker_status()
            self.assertFalse(status["live_trading_enabled"])
            self.assertTrue(status["live_trading_blocked"])
            self.assertEqual(status["paper_broker"], "internal_paper_stub")
            result = broker.block_live_order(MagicMock(), {"symbol": "BTC-USD"})
            self.assertEqual(result["status"], "blocked")
            self.assertFalse(result["live_trading_enabled"])
