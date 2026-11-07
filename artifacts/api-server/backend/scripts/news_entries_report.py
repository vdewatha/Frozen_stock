"""Print the news record so far: news-found vs core trades and the tone-history coverage.

Read-only.  Run against the deployed database (``DATABASE_URL``) to see what
the paper account has learned from news-chosen symbols
(``reports/news-entries-20261106.md``)::

    python scripts/news_entries_report.py [--json]
"""
from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print the raw record instead of Markdown")
    args = parser.parse_args(argv)

    from app.core.config import DEFAULT_PAPER_EXECUTION_SYMBOLS
    from app.db.session import SessionLocal
    from app.services import news_history
    from app.services.trading_universe import ETF_SYMBOLS

    stocks = [symbol for symbol in DEFAULT_PAPER_EXECUTION_SYMBOLS if symbol not in ETF_SYMBOLS]
    record, error = None, None
    try:
        with SessionLocal() as db:
            record = news_history.news_track_record(db)
    except Exception as exc:  # a database without the attribution ledger (fresh local SQLite)
        error = f"{exc.__class__.__name__}: {str(exc).splitlines()[0][:160]}"
    coverage = news_history.coverage_summary(stocks)
    if args.json:
        print(json.dumps({"record": record, "error": error, "coverage": coverage}, indent=1, default=str))
        return 0
    if record is None:
        print(f"No attribution ledger to read: {error}\n")
    else:
        print(f"News record as of {record['as_of']} ({record['discovery_runs']} discovery runs, "
              f"{len(record['news_symbols_ever_admitted'])} symbols ever admitted)\n")
        print(news_history.track_record_table(record))
    covered = [row for row in coverage.values() if row["start"]]
    earliest = min((row["start"] for row in covered), default=None)
    print(f"\nTone history cache: {len(covered)}/{len(coverage)} single stocks covered, earliest {earliest}, "
          f"{sum(row['articles'] for row in covered)} focused articles.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
