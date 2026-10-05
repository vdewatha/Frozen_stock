"""Refresh read-only paper evidence twice and explicitly review restored reads."""
import argparse
import json

from app.core.config import settings
from app.db.session import SessionLocal
from app.services.paper_transport_recovery import review
from app.services.stock_paper_ledger import reconcile_stock_paper_account


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if settings.allow_live_trading or settings.active_paper_broker != "alpaca_paper":
        raise RuntimeError("Only live-disabled Alpaca paper is supported")
    with SessionLocal() as db:
        for _ in range(2):
            reconcile_stock_paper_account(db)
        report = review(db, actor="automated-paper-transport-review", apply=args.apply)
        db.commit()
        print(json.dumps(report))


if __name__ == "__main__":
    main()
