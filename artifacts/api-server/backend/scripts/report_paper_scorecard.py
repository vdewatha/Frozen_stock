"""Print the paper trade scorecard as Markdown (or JSON) from the app database.

Read-only: it opens the configured database, rebuilds closed round trips from
the stock-paper ledger and prints the scorecard.  It never calls the broker,
never places or changes orders and, unless ``--record-snapshot`` is given,
writes nothing.  ``--record-snapshot`` appends the same daily snapshot the
16:20 ET beat job stores (at most one per New York date).

    PYTHONPATH=artifacts/api-server/backend python -m scripts.report_paper_scorecard --days 30
"""
from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=30, help="Window of closed trades by exit time (default 30)")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")
    parser.add_argument("--no-mistakes", action="store_true", help="Skip mistake classification (faster)")
    parser.add_argument("--record-snapshot", action="store_true", help="Also store today's snapshot (one per ET date)")
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error("--days must be at least 1")

    from app.db.session import SessionLocal
    from app.services.paper_trade_scorecard import build_scorecard, record_scorecard_snapshot, render_markdown

    with SessionLocal() as db:
        scorecard = build_scorecard(db, days=args.days, classify=not args.no_mistakes)
        if args.record_snapshot:
            scorecard["snapshot"] = record_scorecard_snapshot(db, days=args.days)
    if args.json:
        json.dump(scorecard, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render_markdown(scorecard))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
