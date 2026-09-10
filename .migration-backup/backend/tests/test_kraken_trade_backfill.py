import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from app.services import kraken_trade_backfill as service


def page(trades, cursor):
    return json.dumps({"error": [], "result": {"XXBTZUSD": [[str(p), str(v), ts, "b", "m", "", i] for i, ts, p, v in trades], "last": str(cursor)}}).encode()


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "research.sqlite"
        self.patch = patch.object(service, "END", service.START + 7200)
        self.patch.start()
        self.db = service.open_backfill(self.path)
    def tearDown(self):
        self.db.close(); self.patch.stop(); self.temp.cleanup()
    def cursor(self): return self.db.execute("SELECT cursor FROM backfill_state").fetchone()[0]

    def test_resume_dedup_complete_and_immutable_export(self):
        start = service.START
        first_cursor = self.cursor()
        raw = page([(1, start + 1, 100, 1), (2, start + 2, 102, 2)], (start + 2) * 10**9)
        service.ingest_page(self.db, first_cursor, raw)
        self.db.close(); self.db = service.open_backfill(self.path)
        self.assertEqual(service.ingest_page(self.db, first_cursor, raw)["status"], "duplicate")
        second = page([(2, start + 2, 102, 2), (3, start + 3601, 99, 3), (4, start + 7201, 98, 4)], (start + 7201) * 10**9)
        self.assertEqual(service.ingest_page(self.db, self.cursor(), second)["status"], "complete")
        output = Path(self.temp.name) / "XBTUSD_60.csv"
        report = service.export_hourly(self.db, output)
        self.assertEqual(report["rows"], 2)
        self.assertTrue(report["source_claim"].startswith("kraken-public-trades-backfill:"))
        self.assertEqual(report, service.export_hourly(self.db, output))
        self.assertEqual(output.read_text().splitlines()[0], f"{start},100,102,100,102,3,2")

    def test_gap_never_fabricated(self):
        start = service.START
        service.ingest_page(self.db, self.cursor(), page([(1, start + 1, 100, 1), (2, start + 7201, 100, 1)], (start + 7201) * 10**9))
        with self.assertRaises(ValueError): service.export_hourly(self.db, Path(self.temp.name) / "XBTUSD_60.csv")

    def test_revised_trade_and_resource_cap_roll_back(self):
        start = service.START
        service.ingest_page(self.db, self.cursor(), page([(1, start + 1, 100, 1)], (start + 1) * 10**9))
        cursor = self.cursor()
        with self.assertRaises(ValueError):
            service.ingest_page(self.db, cursor, page([(1, start + 1, 101, 1), (2, start + 2, 100, 1)], (start + 2) * 10**9))
        self.assertEqual(self.cursor(), cursor)
        with patch.object(service, "MAX_RAW", 1), self.assertRaises(ValueError):
            service.ingest_page(self.db, cursor, page([(2, start + 2, 100, 1)], (start + 2) * 10**9))

    def test_unrelated_database_refused(self):
        other = Path(self.temp.name) / "live.sqlite"
        with sqlite3.connect(other) as db: db.execute("CREATE TABLE users(id INTEGER)")
        with self.assertRaises(ValueError): service.open_backfill(other)

    def test_export_rejects_aggregate_and_raw_tampering(self):
        start = service.START
        raw = page([(1, start + 1, 100, 1), (2, start + 3601, 99, 2), (3, start + 7201, 101, 1)], (start + 7201) * 10**9)
        cursor = self.cursor()
        service.ingest_page(self.db, cursor, raw)
        output = Path(self.temp.name) / "XBTUSD_60.csv"
        self.db.execute("UPDATE backfill_hours SET volume='999' WHERE hour=?", (start,)); self.db.commit()
        with self.assertRaises(ValueError): service.export_hourly(self.db, output)
        self.assertFalse(output.exists())
        self.db.execute("UPDATE backfill_hours SET volume='1' WHERE hour=?", (start,))
        self.db.execute("UPDATE backfill_pages SET raw=?", (b"corrupt",)); self.db.commit()
        with self.assertRaises(ValueError): service.export_hourly(self.db, output)
        self.assertFalse(output.exists())

    def test_cursor_rounding_tolerance(self):
        start = service.START
        raw = page([(1, str(start + 1) + ".1234568", 100, 1)], (start + 1) * 10**9 + 123456750)
        self.assertEqual(service.ingest_page(self.db, self.cursor(), raw)["status"], "collected")
