import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.api import routes
from app.models import Strategy, RiskRule, PaperTrade


class DashboardTests(unittest.TestCase):
    def test_no_ledger_does_not_fabricate_performance_or_trades(self):
        db = MagicMock()
        strategy_query, risk_query, trade_query = MagicMock(), MagicMock(), MagicMock()
        strategy_query.order_by.return_value.all.return_value = [SimpleNamespace(id=1, name="Example", current_status="paper_trading_active")]
        risk_query.first.return_value = None
        trade_query.order_by.return_value.limit.return_value.all.return_value = []
        trade_query.filter.return_value.count.return_value = 0
        db.query.side_effect = lambda model: {Strategy: strategy_query, RiskRule: risk_query, PaperTrade: trade_query}[model]
        with patch.object(routes, "list_strategy_experiments", return_value=[]), patch.object(routes, "propose_parameter_experiments", return_value=[]), patch.object(routes, "latest_market_regime", return_value=None):
            result = routes.dashboard(db)
        self.assertIsNone(result.paper_account_value)
        self.assertIsNone(result.daily_pl)
        self.assertIsNone(result.total_pl)
        self.assertIsNone(result.best_strategy)
        self.assertIsNone(result.worst_strategy)
        self.assertEqual(result.equity_curve, [])
        self.assertEqual(result.recent_trades, [])
        self.assertEqual(result.metrics_status, "unavailable")
        self.assertEqual(result.active_strategies, 1)
        for metric in ("score", "win_rate", "profit_factor", "drawdown"):
            self.assertIsNone(result.strategies[0][metric])


if __name__ == "__main__":
    unittest.main()
