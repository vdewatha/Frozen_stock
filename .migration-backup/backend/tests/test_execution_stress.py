from datetime import datetime, timedelta, timezone
import unittest
from app.services.execution_stress import execution_stress


class ExecutionStressTests(unittest.TestCase):
    def test_isolated_scenarios_and_no_lookahead(self):
        rows = [dict(timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(hours=i),
                     open=100+i, high=102+i, low=99+i, close=101+i, volume=100, tradable=True) for i in range(10)]
        decisions = [1,1,1,0,0,0,1,1,0,0]
        result = execution_stress(rows, decisions)
        self.assertFalse(result["eligible_for_trading"])
        self.assertEqual(len(result["scenarios"]), 5)
        self.assertTrue(all(row["tradable"] for row in rows))
        for scenario in result["scenarios"].values():
            for fill in scenario["fills"]:
                self.assertLess(fill["decision_index"], fill["bar_index"])
        self.assertGreaterEqual(result["scenarios"]["one_extra_bar_delay"]["fills"][0]["bar_index"], 2)
