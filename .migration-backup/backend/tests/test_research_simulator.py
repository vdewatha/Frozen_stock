from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest
from app.services.research_simulator import SimulationConfig, simulate


def bars(count=4):
    return [dict(timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=i),
                 open=100, high=110, low=90, close=100, volume=1000, tradable=True) for i in range(count)]


class SimulatorTests(unittest.TestCase):
    def test_causal_and_last_decision_cannot_fill(self):
        self.assertEqual(simulate(bars(), [0, 0, 0, 1])["fills"], [])
        result = simulate(bars(), [1, 0, 0, 0])
        self.assertEqual(result["fills"][0]["bar_index"], 1)
        self.assertTrue(all(f["bar_index"] == f["decision_index"] + 1 for f in result["fills"]))

    def test_cash_quantity_cost_reconciliation_and_adversity(self):
        result = simulate(bars(), [1, 0, 0, 0])
        cash, qty = Decimal(10000), Decimal(0)
        for fill in result["fills"]:
            if fill["side"] == "buy":
                self.assertGreater(fill["price"], 100)
                cash -= fill["notional"] + fill["fee"]; qty += fill["quantity"]
            else:
                self.assertLess(fill["price"], 100)
                cash += fill["notional"] - fill["fee"]; qty -= fill["quantity"]
            self.assertGreaterEqual(cash, 0); self.assertGreaterEqual(qty, 0)
        self.assertEqual((result["cash"], result["quantity"]), (cash, qty))
        self.assertEqual(result["equity"], cash + qty * 100)
        self.assertEqual(result["total_costs"], result["fees"] + result["spread_costs"] + result["slippage_costs"])
        self.assertEqual(result["completed_trades"], 1)
        self.assertLess(result["total_return"], 0)
        self.assertEqual(result["benchmarks"]["cash"]["total_return"], 0)

    def test_volume_calendar_and_minimum(self):
        rows = bars(); rows[1]["tradable"] = False; rows[2]["volume"] = 0
        result = simulate(rows, [1, 1, 1, 1])
        self.assertEqual(result["fills"], [])
        partial = simulate(bars(), [1]*4)["fills"][0]
        self.assertEqual(partial["quantity"], 10)
        self.assertTrue(partial["partial"])
        self.assertEqual(simulate(bars(), [1]*4, SimulationConfig(minimum_notional=20000))["fills"], [])

    def test_allocation_and_no_overdraw_across_gaps(self):
        rows = bars(50)
        for i, row in enumerate(rows):
            row.update(open=100+i*10, close=100+i*10, high=110+i*10, low=90+i*10)
        result = simulate(rows, [i % 2 for i in range(50)], SimulationConfig(allocation="0.5", lot_step="0.1"))
        self.assertTrue(all(point["cash"] >= 0 and point["quantity"] >= 0 for point in result["equity_curve"]))
        self.assertTrue(all(fill["quantity"] % Decimal("0.1") == 0 for fill in result["fills"]))

    def test_malformed_inputs(self):
        for field, value in [("open", float("nan")), ("volume", -1), ("high", 1), ("tradable", 1), ("timestamp", datetime(2026, 1, 1))]:
            rows = bars(); rows[1][field] = value
            with self.assertRaises(ValueError): simulate(rows, [1]*4)
        rows = bars(); rows[1]["timestamp"] = rows[0]["timestamp"]
        with self.assertRaises(ValueError): simulate(rows, [1]*4)
        with self.assertRaises(ValueError): simulate(bars(), [True]*4)
        with self.assertRaises(ValueError): SimulationConfig(allocation="1.1")

    def test_buyhold_keeps_initial_quantity_after_price_decline(self):
        rows = bars()
        for row in rows:
            row["volume"] = 100000
        rows[2].update(open=50, high=55, low=45, close=50)
        rows[3].update(open=50, high=55, low=45, close=50)
        result = simulate(rows, [1]*4, SimulationConfig(allocation="0.5", fee_bps=0, spread_bps=0, slippage_bps=0))
        benchmark = result["benchmarks"]["buy_and_hold"]
        self.assertEqual(benchmark["quantity"], 50)
        self.assertEqual(benchmark["fill_count"], 1)
        self.assertGreater(result["quantity"], benchmark["quantity"])

    def test_current_volume_cannot_increase_prior_capacity(self):
        rows = bars()
        baseline = simulate(rows, [1, 0, 0, 0])["fills"][0]["quantity"]
        rows[1]["volume"] = 1000000
        increased = simulate(rows, [1, 0, 0, 0])["fills"][0]["quantity"]
        self.assertEqual(baseline, increased)

    def test_buyhold_partial_fills_stop_at_fixed_target(self):
        rows = bars(8)
        for row in rows[2:]:
            row.update(open=50, high=55, low=45, close=50)
        result = simulate(rows, [1]*8, SimulationConfig(allocation="0.5", fee_bps=0, spread_bps=0, slippage_bps=0))
        benchmark = result["benchmarks"]["buy_and_hold"]
        self.assertEqual(benchmark["quantity"], 50)
        self.assertEqual(benchmark["fill_count"], 5)
