from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from app.services import kraken_trade_backfill as backfill
from app.services import kraken_gap_audit as audit
from app.services.kraken_history import read_kraken_csv
from test_kraken_trade_backfill import page


class GapAuditTests(unittest.TestCase):
    def test_verified_gap_export_keeps_real_rows_only(self):
        start = backfill.START
        end = start + 10800
        with tempfile.TemporaryDirectory() as temp, patch.object(backfill, 'END', end), patch.object(audit, 'END', end):
            root = Path(temp)
            db = backfill.open_backfill(root / 'history.sqlite')
            try:
                raw = page([(1, start+1, 100, 1), (2, start+7201, 101, 2), (3, end+1, 102, 1)], (end+1)*10**9)
                cursor = db.execute('SELECT cursor FROM backfill_state').fetchone()[0]
                backfill.ingest_page(db, cursor, raw)
                probe = page([(2, start+7201, 101, 2), (3, end+1, 102, 1)], (end+1)*10**9)
                evidence = audit.build_audit(db, fetcher=lambda _: probe)
                path = root / 'gap-audit.json'
                path.write_text(json.dumps(evidence))
                self.assertEqual(audit.verify_audit(db, path)[0], [start+3600])
                csv = root / 'XBTUSD_60.csv'
                with self.assertRaises(ValueError): backfill.export_hourly(db, csv)
                report = backfill.export_hourly(db, csv, gap_audit=path)
                self.assertEqual(report['rows'], 2)
                bounds = dict(start=datetime.fromtimestamp(start, timezone.utc), end=datetime.fromtimestamp(end, timezone.utc))
                with self.assertRaises(ValueError): read_kraken_csv(csv, **bounds)
                frame, _ = read_kraken_csv(csv, **bounds, allowed_missing_hours=[start+3600])
                self.assertEqual(len(frame), 2)
                self.assertEqual(list(frame.volume), [1, 2])
                with self.assertRaises(ValueError): read_kraken_csv(csv, **bounds, allowed_missing_hours=[start])
                original = json.dumps(evidence)
                evidence['probes'][0]['before'][0] = 999
                path.write_text(json.dumps(evidence))
                with self.assertRaises(ValueError): audit.verify_audit(db, path)
                evidence = json.loads(original)
                evidence['missing_hours'].append(start+7200)
                path.write_text(json.dumps(evidence))
                with self.assertRaises(ValueError): audit.verify_audit(db, path)
                evidence = json.loads(original)
                evidence['probes'][0]['response'] = '{}'
                path.write_text(json.dumps(evidence))
                with self.assertRaises(ValueError): audit.verify_audit(db, path)
            finally:
                db.close()

    def test_probe_cannot_skip_an_existing_trade(self):
        start = backfill.START
        with tempfile.TemporaryDirectory() as temp, patch.object(backfill, 'END', start+10800), patch.object(audit, 'END', start+10800):
            db = backfill.open_backfill(Path(temp) / 'history.sqlite')
            try:
                raw = page([(1, start+1, 100, 1), (3, start+7201, 101, 2), (4, start+10801, 102, 1)], (start+10801)*10**9)
                backfill.ingest_page(db, db.execute('SELECT cursor FROM backfill_state').fetchone()[0], raw)
                with self.assertRaises(ValueError):
                    audit.build_audit(db, fetcher=lambda _: page([(3,start+7201,101,2)],(start+7201)*10**9))
            finally:
                db.close()
