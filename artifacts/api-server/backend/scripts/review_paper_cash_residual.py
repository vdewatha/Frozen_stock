"""Replay a fresh paper journal and review only the explained cash discrepancy."""
import argparse
import json

from app.core.config import settings
from app.db.schema import assert_schema_current
from app.db.session import SessionLocal, engine
from app.services.paper_cash_policy import review_cent_residual
from app.services.stock_paper_ledger import reconcile_stock_paper_account


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", required=True, choices=["MONETARY_REVIEW_NOT_TRADING_APPROVAL"])
    args = parser.parse_args()
    if settings.allow_live_trading or settings.active_paper_broker != "alpaca_paper":
        raise RuntimeError("This command requires live-disabled Alpaca paper")
    assert_schema_current(engine)
    with SessionLocal() as db:
        reconcile_stock_paper_account(db)
        report = review_cent_residual(db, actor="paper-monetary-review", apply=args.apply)
        if args.apply:
            db.commit()
        print(json.dumps(report))


if __name__ == "__main__":
    main()
