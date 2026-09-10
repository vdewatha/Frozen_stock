from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from concurrent.futures import ThreadPoolExecutor
import unittest
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session
from app.db.base import Base
from app.integrations.kraken import BTC_USD
from app.services.instruments import Candle, Timeframe
from app.models.crypto_data import CryptoCandle, CollectionRun
from app.services.crypto_collection import collect_kraken, load_closed_history, CryptoDataError

CLOCK = datetime(2026, 9, 4, 12, 5, tzinfo=timezone.utc)


class Client:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else [Candle(BTC_USD, Timeframe.HOUR,
            CLOCK.replace(minute=0)-timedelta(hours=i), 100, 102, 99, 101, 3) for i in range(120, 0, -1)]
    def ohlc(self, *args, **kwargs):
        return self.rows


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.engine = create_engine("sqlite:///" + self.temp.name + "/test.db", connect_args={"timeout": 10})
        Base.metadata.create_all(self.engine)
    def tearDown(self):
        self.engine.dispose()
        self.temp.cleanup()
    def test_idempotent_and_available(self):
        with Session(self.engine) as db:
            self.assertEqual(collect_kraken(db, Client(), CLOCK).inserted_count, 120)
            db.commit()
            self.assertEqual(collect_kraken(db, Client(), CLOCK).inserted_count, 0)
            self.assertEqual(len(load_closed_history(db, as_of=CLOCK)), 120)
            with self.assertRaises(CryptoDataError):
                load_closed_history(db, as_of=CLOCK-timedelta(minutes=1))
    def test_revision_and_gap_rejected(self):
        with Session(self.engine) as db:
            collect_kraken(db, Client(), CLOCK)
            db.commit()
            rows = Client().rows
            row = rows[-1]
            rows[-1] = Candle(BTC_USD, Timeframe.HOUR, row.opened_at, 100, 102, 99, 100, 3)
            self.assertEqual(collect_kraken(db, Client(rows), CLOCK).error_code, "provider_revision")
            self.assertEqual(collect_kraken(db, Client(rows[:-2]+rows[-1:]), CLOCK).error_code, "history_gap")
            self.assertEqual(float(load_closed_history(db, as_of=CLOCK)[-1].close), 101)
    def test_stale_and_boundary_gap(self):
        with Session(self.engine) as db:
            self.assertEqual(collect_kraken(db, Client(Client().rows[:-1]), CLOCK).error_code, "stale_history")
            collect_kraken(db, Client(), CLOCK)
            db.commit()
            later = CLOCK+timedelta(hours=3)
            rows = [Candle(BTC_USD, Timeframe.HOUR, later.replace(minute=0)-timedelta(hours=1), 100, 102, 99, 101, 3)]
            self.assertEqual(collect_kraken(db, Client(rows), later).error_code, "history_gap")
            self.assertEqual(db.scalar(select(func.count()).select_from(CryptoCandle)), 120)
    def test_caller_rollback(self):
        with Session(self.engine) as db:
            collect_kraken(db, Client(), CLOCK)
            db.rollback()
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(CryptoCandle)), 0)
            self.assertEqual(db.scalar(select(func.count()).select_from(CollectionRun)), 0)
    def test_revision_rolls_back_new_prefix_and_audits(self):
        with Session(self.engine) as db:
            collect_kraken(db, Client(), CLOCK)
            db.commit()
            rows = Client().rows
            prefix = Candle(BTC_USD, Timeframe.HOUR, rows[0].opened_at-timedelta(hours=1), 100, 102, 99, 101, 3)
            old = rows[-1]
            rows[-1] = Candle(BTC_USD, Timeframe.HOUR, old.opened_at, 100, 102, 99, 100, 3)
            run = collect_kraken(db, Client([prefix]+rows), CLOCK)
            db.commit()
            self.assertEqual(run.error_code, "provider_revision")
            self.assertEqual(run.inserted_count, 0)
            self.assertEqual(db.scalar(select(func.count()).select_from(CryptoCandle)), 120)
            self.assertEqual(db.scalar(select(func.count()).select_from(CollectionRun)), 2)

    def test_stored_integrity_future_and_insufficient_fail_closed(self):
        with Session(self.engine) as db:
            with self.assertRaises(CryptoDataError):
                load_closed_history(db, as_of=CLOCK)
            collect_kraken(db, Client(), CLOCK)
            row = db.scalar(select(CryptoCandle))
            row.close = 100
            db.flush()
            with self.assertRaisesRegex(CryptoDataError, "stored_integrity"):
                load_closed_history(db, as_of=CLOCK)
        future = Candle(BTC_USD, Timeframe.HOUR, CLOCK.replace(minute=0), 100, 102, 99, 101, 3)
        with Session(self.engine) as db:
            self.assertEqual(collect_kraken(db, Client([future]), CLOCK).error_code, "future_or_unaligned_candle")
    def test_concurrent_insert(self):
        def execute(_):
            with Session(self.engine) as db:
                run = collect_kraken(db, Client(), CLOCK)
                result = (run.status, run.inserted_count)
                db.commit()
                return result
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(execute, range(2)))
        self.assertEqual(sorted(results), [("success", 0), ("success", 120)])
