from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.models.crypto_data import CryptoCandle
from app.services.crypto_collection import _hash, _values
from app.services.instruments import Candle, Timeframe
from app.integrations.kraken import BTC_USD
from app.services.kraken_history import (read_kraken_csv, import_kraken_history, merge_verified_history,
                                        load_history_bundle, SOURCE_URL)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "XBTUSD_60.csv"
        self.start = datetime(2023, 1, 1, tzinfo=timezone.utc)
        self.clock = self.start + timedelta(days=366)
    def tearDown(self):
        self.temp.cleanup()
    def write(self, count=48):
        rows = [f"{int((self.start+timedelta(hours=i)).timestamp())},100,102,99,101,3,2\n" for i in range(count)]
        self.path.write_text("".join(rows))
        return rows
    def test_year_roundtrip_hashes_and_immutable(self):
        self.write(8760)
        manifest = import_kraken_history(self.path, self.root/"out", source_url=SOURCE_URL, as_of=self.clock)
        folder = self.root/"out"/manifest["snapshot_id"]
        frame, loaded = load_history_bundle(folder)
        self.assertEqual(loaded, manifest)
        self.assertEqual(len(frame), 8760)
        self.assertFalse(loaded["provenance_authenticated"])
        with self.assertRaises(FileExistsError):
            import_kraken_history(self.path, self.root/"out", source_url=SOURCE_URL, as_of=self.clock)
        (folder/"dataset.csv").write_text("tampered")
        with self.assertRaisesRegex(ValueError, "checksum"):
            load_history_bundle(folder)
    def test_missing_duplicate_future_invalid_ohlc_nan(self):
        good = self.write()
        cases = [good[:3]+good[4:], good[:3]+good[2:], [good[0].replace(",100,", ",NaN,", 1)],
                 [good[0].replace(",102,", ",90,", 1)], [good[0].replace(",3,2", ",-1,2")],
                 [good[0].replace(",3,2", ",3,1.1")], [good[0].replace(good[0].split(",")[0], str(int(self.clock.timestamp())))]]
        for rows in cases:
            with self.subTest(rows=rows[:1]):
                self.path.write_text("".join(rows))
                with self.assertRaises(ValueError):
                    read_kraken_csv(self.path, as_of=self.clock)
    def test_identity_source_and_minimum(self):
        self.write()
        with self.assertRaises(ValueError):
            import_kraken_history(self.path, self.root/"out", source_url="https://example.com", as_of=self.clock)
        with self.assertRaises(ValueError):
            import_kraken_history(self.path, self.root/"out", source_url=SOURCE_URL, as_of=self.clock)
        bad = self.root/"ETHUSD_60.csv"
        self.path.rename(bad)
        with self.assertRaises(ValueError):
            read_kraken_csv(bad, as_of=self.clock)
    def live(self, index, close=101):
        candle = Candle(BTC_USD, Timeframe.HOUR, self.start+timedelta(hours=index),100,102,99,close,3)
        values = _values(candle)
        return CryptoCandle(**values, observed_at=(self.start+timedelta(hours=50)).isoformat(),content_sha256=_hash(values))
    def test_exact_live_overlap_and_conflict_no_db_mutation(self):
        self.write()
        clock = self.start+timedelta(hours=50)
        frame, _ = read_kraken_csv(self.path, as_of=clock)
        rows = [self.live(i) for i in range(46,50)]
        merged = merge_verified_history(frame, rows, as_of=clock)
        self.assertEqual(len(merged),50)
        rows[0] = self.live(46,100)
        with self.assertRaisesRegex(ValueError,"conflict"):
            merge_verified_history(frame, rows, as_of=clock)
        with self.assertRaisesRegex(ValueError,"overlapping"):
            merge_verified_history(frame,[self.live(49)],as_of=clock)
        rows = [self.live(i) for i in range(46,50)]
        manifest = import_kraken_history(self.path,self.root/"out",source_url=SOURCE_URL,
            as_of=clock,live_rows=rows,minimum_days=2)
        self.assertEqual(len(load_history_bundle(self.root/"out"/manifest["snapshot_id"])[0]),50)

    def test_explicit_window_ignores_only_outside_gaps(self):
        rows = self.write(96)
        start, end = self.start+timedelta(hours=24), self.start+timedelta(hours=72)
        self.path.write_text("".join(rows[:2]+rows[3:]))
        frame, _ = read_kraken_csv(self.path, as_of=self.clock, start=start, end=end)
        self.assertEqual(len(frame), 48)
        manifest = import_kraken_history(self.path, self.root/"out",source_url=SOURCE_URL,as_of=self.clock,
                                       start=start,end=end,minimum_days=2)
        self.assertEqual(manifest["selection_start"],start.isoformat())
        self.assertEqual(manifest["selection_end_exclusive"],end.isoformat())
        self.assertEqual(len(load_history_bundle(self.root/"out"/manifest["snapshot_id"])[0]),48)
        self.path.write_text("".join(rows[:30]+rows[31:]))
        with self.assertRaises(ValueError):
            read_kraken_csv(self.path,as_of=self.clock,start=start,end=end)
        self.path.write_text("".join(rows[25:]))
        with self.assertRaisesRegex(ValueError,"boundary"):
            read_kraken_csv(self.path,as_of=self.clock,start=start,end=end)
