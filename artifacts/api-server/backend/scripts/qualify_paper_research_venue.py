"""Record automated paper accounting evidence, without activating execution."""
import json

from app.core.config import settings
from app.db.session import SessionLocal
from app.services.paper_research_venue import record_qualification, status
from app.services.stock_paper_ledger import active_paper_account, reconcile_stock_paper_account


def main():
    if settings.allow_live_trading or settings.active_paper_broker != "alpaca_paper":
        raise RuntimeError("Only live-disabled Alpaca paper is supported")
    with SessionLocal() as db:
        reconcile_stock_paper_account(db)
        account = active_paper_account(db, for_update=True)
        record_qualification(db, account, reviewer="automated-paper-accounting-review")
        report = status(db, account)
        db.commit()
        print(json.dumps(report))
    return 0 if report["qualified_for_observed_start"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
