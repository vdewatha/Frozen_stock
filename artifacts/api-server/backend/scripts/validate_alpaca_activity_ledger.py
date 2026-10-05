"""Exercise real broker evidence in a disposable ledger; no order/account writes."""
from datetime import UTC, datetime
import json
from pathlib import Path
import tempfile

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models
from app.core.config import settings
from app.db.base import Base
from app.models.stock_paper import StockPaperBrokerActivity, StockPaperFill, StockPaperLedgerEvent
from app.services.alpaca_activity_v2 import VERSION
from app.services.stock_paper_ledger import StockPaperError, initialize_stock_paper_account, reconcile_stock_paper_account
from scripts.validate_alpaca_paper_evidence import ReadOnlyCandidate


def replay_matches(reads):
    """Matching balances alone do not establish an unchanged activity replay."""
    if len(reads) != 2:
        return False
    def evidence(row):
        reconciliation = row.get("reconciliation") or {}
        return (row.get("activity_count"), row.get("fill_count"),
                reconciliation.get("event_count"), reconciliation.get("journal_sha256"),
                reconciliation.get("baseline_journal_sha256"))
    return bool((reads[0].get("reconciliation") or {}).get("journal_sha256")) and evidence(reads[0]) == evidence(reads[1])


def validate():
    """Never connect the scratch session to the configured application database."""
    prior_provider = settings.active_paper_broker
    settings.active_paper_broker = "alpaca_paper"
    try:
        with tempfile.TemporaryDirectory(prefix="alpaca-ledger-probe-") as directory:
            engine = create_engine("sqlite:///" + str(Path(directory) / "scratch.sqlite"))
            try:
                Base.metadata.create_all(engine)
                with Session(engine) as db:
                    initialized = initialize_stock_paper_account(db, ReadOnlyCandidate(), activity_contract=VERSION)
                reads = []
                # New DB sessions and API clients exercise replay after process-state loss.
                for _ in range(2):
                    with Session(engine) as db:
                        status = reconcile_stock_paper_account(db, ReadOnlyCandidate())
                        event = db.query(StockPaperLedgerEvent).filter_by(event_type="activity_reconciliation").order_by(StockPaperLedgerEvent.id.desc()).first()
                        reads.append({"status": status["status"], "costs_known": status["costs_known"],
                                      "accounting_verified": status["account"]["accounting_verified"],
                                      "activity_count": db.query(StockPaperBrokerActivity).count(),
                                      "fill_count": db.query(StockPaperFill).count(),
                                      "reconciliation": event.payload if event else None})
                unchanged = replay_matches(reads)
                passed = unchanged and initialized["status"] == "reconciled" and all(
                    row["status"] == "reconciled" and row["reconciliation"] is not None
                    and row["reconciliation"]["status"] == "matched"
                    and not row["costs_known"] and not row["accounting_verified"] for row in reads
                )
                return {"observed_at": datetime.now(UTC).isoformat(), "status": "passed" if passed else "blocked",
                        "broker_access": "allowlisted_GET_only", "database": "disposable_sqlite",
                        "application_database_modified": False, "initialization_status": initialized["status"],
                        "reads": reads, "replay_unchanged": unchanged,
                        "qualification": "not_established", "launch_authorized": False}
            finally:
                engine.dispose()
    finally:
        settings.active_paper_broker = prior_provider


def main():
    try:
        report = validate()
    except StockPaperError as exc:
        # Adapter errors are sanitized; never emit raw broker response bodies.
        report = {"status": "blocked", "reason": str(exc), "launch_authorized": False}
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
