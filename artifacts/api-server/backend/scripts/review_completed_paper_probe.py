"""Explicit completed-probe review. Never submit orders or resume trading."""
import argparse
import fcntl
import json
import os
from pathlib import Path
from uuid import UUID

from app.core.config import settings
from app.db.session import SessionLocal
from app.services.paper_probe_review import review
from app.services.stock_paper_ledger import AlpacaPaperClient, reconcile_stock_paper_account
from scripts.test_alpaca_paper_roundtrip import SYMBOLS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True, type=UUID)
    parser.add_argument("--symbol", required=True, choices=SYMBOLS)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", choices=["PROBE_REVIEW_NOT_TRADING_APPROVAL"])
    args = parser.parse_args()
    if settings.allow_live_trading or settings.active_paper_broker != "alpaca_paper":
        raise RuntimeError("Only live-disabled Alpaca paper is supported")
    if args.apply and args.confirm != "PROBE_REVIEW_NOT_TRADING_APPROVAL":
        parser.error("Applying requires --confirm PROBE_REVIEW_NOT_TRADING_APPROVAL")
    path = Path("/data/reports") / f"paper-roundtrip-{args.run_id}.jsonl"
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        if os.fstat(stream.fileno()).st_size > 1_000_000:
            raise RuntimeError("Probe journal is unexpectedly large")
        content = stream.read()
        with SessionLocal() as db:
            client = AlpacaPaperClient()
            reconcile_stock_paper_account(db, client)
            result = review(db, client, content, run_id=str(args.run_id), symbol=args.symbol,
                            actor="explicit-paper-probe-review", apply=args.apply)
            if args.apply:
                db.commit()
            print(json.dumps(result))


if __name__ == "__main__":
    main()
