from datetime import datetime,timedelta,timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import unittest

from app.models.crypto_data import CryptoCandle
from app.integrations.kraken import BTC_USD
from app.services.crypto_collection import _values,_hash
from app.services.instruments import Candle,Timeframe
from app.services.paper_comparison import advance_comparison,ComparisonConfig


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory()
        self.path=Path(self.temp.name)/"comparison.sqlite"
        self.start=datetime(2025,1,1,tzinfo=timezone.utc)
    def tearDown(self):
        self.temp.cleanup()
    def rows(self,count=60,latest=None):
        rows=[]
        for i in range(count):
            price=Decimal(100)+i
            if latest is not None and i==count-1:
                price=Decimal(str(latest))
            candle=Candle(BTC_USD,Timeframe.HOUR,self.start+timedelta(hours=i),float(price),float(price+2),float(price-2),float(price),3)
            values=_values(candle)
            rows.append(CryptoCandle(**values,content_sha256=_hash(values),
                observed_at=(self.start+timedelta(hours=max(60,i+1),minutes=1)).isoformat()))
        return rows
    def step(self,count=60,**kwargs):
        return advance_comparison(self.path,self.rows(count),observed_at=self.start+timedelta(hours=count,minutes=1),**kwargs)
    def account(self,report,strategy):
        return next(row for row in report["accounts"] if row["strategy"]==strategy)
    def test_forward_fill_not_retroactive_and_costs(self):
        first=self.step()
        self.assertTrue(all(a["fill"] is None for a in first["accounts"]))
        self.assertEqual(self.account(first,"cost_ml")["reason"],"model_unavailable")
        again=self.step()
        self.assertEqual(first,again)
        second=self.step(61)
        trend=self.account(second,"trend")
        self.assertEqual(trend["fill"]["side"],"buy")
        self.assertEqual(Decimal(trend["fill"]["price"]),Decimal("160.160"))
        self.assertGreater(Decimal(trend["fill"]["fee"]),0)
        self.assertEqual(trend["state"]["fills"],1)
        self.assertLess(Decimal(trend["equity"]),Decimal(10000))
        self.assertEqual(self.account(second,"cash")["equity"],"10000.00000000")
        self.assertFalse(second["eligible_for_qualification"])
    def test_ml_expected_net_gate_and_horizon_exit(self):
        config=ComparisonConfig(ml_model_id="fixture-model",ml_horizon_hours=1)
        first=self.step(config=config,ml_probability=.9,ml_expected_net=-.01)
        self.assertEqual(self.account(first,"cost_ml")["action"],"hold")
        second=self.step(61,config=config,ml_probability=.9,ml_expected_net=.02)
        self.assertEqual(self.account(second,"cost_ml")["action"],"buy")
        third=self.step(62,config=config,ml_probability=.9,ml_expected_net=.02)
        self.assertEqual(self.account(third,"cost_ml")["fill"]["side"],"buy")
        self.assertEqual(self.account(third,"cost_ml")["reason"],"model_horizon_exit")
        fourth=self.step(63,config=config,ml_probability=.9,ml_expected_net=.02)
        self.assertEqual(self.account(fourth,"cost_ml")["fill"]["side"],"sell")
    def test_missing_model_cancels_pending_entry(self):
        config=ComparisonConfig(ml_model_id="fixture-model")
        self.step(config=config,ml_probability=.9,ml_expected_net=.02)
        result=self.step(61,config=config)
        ml=self.account(result,"cost_ml")
        self.assertIsNone(ml["fill"])
        self.assertEqual(ml["reason"],"model_unavailable")
    def test_late_gap_and_changed_configuration_fail_closed(self):
        self.step()
        late=advance_comparison(self.path,self.rows(61),observed_at=self.start+timedelta(hours=61,minutes=10))
        self.assertEqual(late["status"],"blocked")
        after=self.step(62)
        self.assertTrue(self.account(after,"trend")["pending_cancelled_for_gap"])
        self.assertTrue(all(a["fill"] is None for a in after["accounts"]))
        with self.assertRaisesRegex(ValueError,"frozen"):
            self.step(63,config=ComparisonConfig(fee_rate=".01",fee_profile="stress"))
    def test_unrelated_database_never_modified(self):
        with sqlite3.connect(self.path) as connection:
            connection.execute("CREATE TABLE users (id INTEGER)")
        with self.assertRaisesRegex(ValueError,"non-comparison"):
            self.step()
        with sqlite3.connect(self.path) as connection:
            self.assertEqual([r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")],["users"])
    def test_concurrent_idempotent_and_revision_rejection(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            results=list(executor.map(lambda _:self.step(),range(2)))
        self.assertEqual(results[0],results[1])
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM comparison_steps").fetchone()[0],1)
        with self.assertRaisesRegex(ValueError,"revised"):
            advance_comparison(self.path,self.rows(latest=158),observed_at=self.start+timedelta(hours=60,minutes=1))
    def test_no_pyramid_and_drawdown_halt(self):
        config=ComparisonConfig(maximum_drawdown=".000001")
        self.step(config=config)
        bought=self.step(61,config=config)
        trend=self.account(bought,"trend")
        self.assertTrue(trend["state"]["halted"])
        self.assertEqual(trend["action"],"sell")
        sold=self.step(62,config=config)
        self.assertEqual(self.account(sold,"trend")["fill"]["side"],"sell")
        self.assertEqual(self.account(sold,"trend")["action"],"hold")

    def test_missing_strategy_and_corrupted_balances_rejected(self):
        self.step()
        with sqlite3.connect(self.path) as connection:
            connection.execute("DELETE FROM comparison_accounts WHERE strategy='cash'")
        with self.assertRaisesRegex(ValueError,"incomplete"):
            self.step(61)
        other=Path(self.temp.name)/"other.sqlite"
        advance_comparison(other,self.rows(),observed_at=self.start+timedelta(hours=60,minutes=1))
        with sqlite3.connect(other) as connection:
            connection.execute("UPDATE comparison_accounts SET state='{}' WHERE strategy='cash'")
        with self.assertRaisesRegex(ValueError,"committed"):
            advance_comparison(other,self.rows(61),observed_at=self.start+timedelta(hours=61,minutes=1))
