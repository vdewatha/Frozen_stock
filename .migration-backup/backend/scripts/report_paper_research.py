"""Read-only status of the selected paper database; never orders or approvals."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approval-id", type=int, required=True)
    parser.add_argument("--binding-id", type=int, required=True)
    args = parser.parse_args()
    if not os.environ.get("DATABASE_URL"):
        parser.error("Explicit DATABASE_URL required")
    from sqlalchemy import select
    from app.db.session import engine, SessionLocal
    from app.db.schema import assert_schema_current
    from app.models.crypto_data import CollectionRun
    from app.models.execution import PaperExecutionAccount
    from app.services.paper_performance import paper_performance
    from app.services.shadow_evidence import shadow_evidence_report
    assert_schema_current(engine)
    try:
        with SessionLocal() as db:
            last = db.scalar(select(CollectionRun).order_by(CollectionRun.id.desc()).limit(1))
            account = db.get(PaperExecutionAccount, 1)
            result = {"performance": paper_performance(db, args.approval_id),
                "evidence": shadow_evidence_report(db, args.binding_id),
                "collection": {"status": last.status, "observed_at": last.observed_at, "error": last.error_code} if last else None,
                "account": {"cash": str(account.cash), "quantity": str(account.quantity),
                    "reserved_cash": str(account.reserved_cash), "kill_switch": account.kill_switch} if account else None}
            print(json.dumps(result, sort_keys=True))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
