"""Collect public-API evidence for missing historical hours, without DB writes."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.kraken_gap_audit import build_audit
from app.services.kraken_trade_backfill import fetch_page


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.database.is_symlink() or not args.database.is_file():
        parser.error('Existing regular historical DB required')
    def paced(cursor):
        time.sleep(1)
        return fetch_page(cursor)
    with sqlite3.connect(args.database.resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.execute('PRAGMA query_only=ON')
        audit = build_audit(db, fetcher=paced)
    with args.output.open('x') as handle:
        json.dump(audit, handle, sort_keys=True, indent=2)
    print(json.dumps({'missing_hours': audit['missing_hours'], 'status': 'audited_no_trade_gaps'}))


if __name__ == '__main__':
    main()
