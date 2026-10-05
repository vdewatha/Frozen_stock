"""Refresh paper evidence and record a scoped accounting check, never an approval."""
import json

from app.core.config import settings
from app.db.session import SessionLocal
from app.services.paper_research_accounting import assess
from app.services.stock_paper_ledger import active_paper_account, reconcile_stock_paper_account, _event


def main():
    if settings.allow_live_trading or settings.active_paper_broker != "alpaca_paper":
        raise RuntimeError("Only live-disabled Alpaca paper is supported")
    with SessionLocal() as db:
        reconcile_stock_paper_account(db)
        account = active_paper_account(db, for_update=True)
        report = assess(db, account)
        db.info["stock_paper_actor"] = "paper-research-accounting-check"
        _event(db, account, "paper_research_accounting_check", report["status"], report["reason"], report)
        db.commit()
        print(json.dumps(report))
    return 0 if report["accounting_observation_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
