import unittest
from unittest.mock import Mock
from app.integrations.freqtrade import FreqtradeError
from app.integrations.freqtrade_execution import FreqtradeDryRunClient


class HistoryTests(unittest.TestCase):
    def client(self):
        client = FreqtradeDryRunClient("http://localhost:8080", "user", "password")
        self.addCleanup(client.close)
        client.verify = Mock()
        client.trades = Mock(return_value=[{"trade_id": 102}])
        return client

    def test_pagination_includes_open_and_closed(self):
        client = self.client()
        client._get = Mock(side_effect=[
            {"trades": [{"trade_id": i} for i in range(1, 101)], "trades_count": 100, "offset": 0, "total_trades": 101},
            {"trades": [{"trade_id": 101}], "trades_count": 1, "offset": 100, "total_trades": 101},
        ])
        self.assertEqual(len(client.history()), 102)
        self.assertIn("offset=100", client._get.call_args.args[0])

    def test_bound_and_truncation_fail_closed(self):
        for page in (
            {"trades": [], "trades_count": 0, "offset": 0, "total_trades": 1001},
            {"trades": [], "trades_count": 0, "offset": 0, "total_trades": 1},
            {"trades": [{"trade_id": 102}], "trades_count": 1, "offset": 0, "total_trades": 1},
        ):
            client = self.client()
            client._get = Mock(return_value=page)
            with self.assertRaises(FreqtradeError):
                client.history()

    def test_changed_total_fails_closed(self):
        client = self.client()
        client._get = Mock(side_effect=[
            {"trades": [{"trade_id": 1}], "trades_count": 1, "offset": 0, "total_trades": 2},
            {"trades": [{"trade_id": 2}], "trades_count": 1, "offset": 1, "total_trades": 3},
        ])
        with self.assertRaises(FreqtradeError):
            client.history()
