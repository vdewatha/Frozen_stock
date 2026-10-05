"""Fresh read-only broker reconciliation, then explicit local monetary-policy adoption."""
import argparse
import json

from app.core.config import settings
from app.db.session import SessionLocal, engine
from app.db.schema import assert_schema_current
from app.services.paper_cash_policy import adopt_cent_policy
from app.services.stock_paper_ledger import reconcile_stock_paper_account


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", required=True, choices=["PAPER_CASH_ROUNDING_ONLY"])
    args = parser.parse_args()
    if settings.allow_live_trading or settings.active_paper_broker != "alpaca_paper":
        raise RuntimeError("This command is restricted to live-disabled Alpaca paper")
    assert_schema_current(engine)
    with SessionLocal() as db:
        reconcile_stock_paper_account(db)
        report = adopt_cent_policy(db, actor="paper-cash-policy-review", apply=args.apply)
        if args.apply:
            db.commit()
        print(json.dumps(report))


if __name__ == "__main__":
    main()
