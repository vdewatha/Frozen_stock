import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from app.api import routes
from app.schemas.trading import BacktestRequest, ModelPredictionRequest, SignalRequest
from app.services.trusted_data import UntrustedMarketData


class ResearchRouteTests(unittest.TestCase):
    def test_research_endpoints_block_instead_of_generating_demo_prices(self):
        cases = [
            (routes.generate_signal, SignalRequest(symbol="SPY", strategy="moving_average_crossover"), 260, 60),
            (routes.backtest, BacktestRequest(), 320, 80),
            (routes.model_prediction, ModelPredictionRequest(), 420, 140),
        ]
        for endpoint, payload, limit, minimum in cases:
            with self.subTest(endpoint=endpoint.__name__):
                db = MagicMock()
                with patch.object(routes, "trusted_history", side_effect=UntrustedMarketData("untrusted history")) as read, patch.object(routes, "predict_probabilities") as predict, patch.object(routes, "run_backtest") as backtest:
                    with self.assertRaises(HTTPException) as result:
                        endpoint(payload, db)
                self.assertEqual(result.exception.status_code, 409)
                self.assertEqual(result.exception.detail["status"], "blocked")
                read.assert_called_once_with(db, "SPY", limit, minimum=minimum)
                predict.assert_not_called()
                backtest.assert_not_called()
                db.add.assert_not_called()


if __name__ == "__main__":
    unittest.main()
