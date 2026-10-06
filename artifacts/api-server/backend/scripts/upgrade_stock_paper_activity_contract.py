"""Safely upgrade an existing Alpaca paper ledger to the v2 activity contract.

The default operation is a read-only preflight.  Applying requires an explicit
confirmation token and only changes the local ledger contract; this script has
no broker write calls and never enables trading.
"""
import argparse
import hashlib
import json
from datetime import datetime, timezone

from app.core.config import settings
from app.db.session import SessionLocal
from app.models import StockPaperAccount, StockPaperLedgerEvent
from app.services import alpaca_activity_v2
from app.services.stock_paper_ledger import (
    NONTERMINAL_ORDER_STATUSES,
    _snapshot,
    _v2_snapshot_key,
    active_paper_gateway,
)


CONFIRM = "UPGRADE_ALPACA_ACTIVITY_CONTRACT_V2"


def _report(account, first, second):
    reasons = []
    try:
        stable = _v2_snapshot_key(first) == _v2_snapshot_key(second)
    except (TypeError, ValueError, KeyError) as exc:
        stable = False
        reasons.append(f"Broker snapshot is not valid v2 evidence: {exc}")
    raw_account, positions, orders, activities, observed = second
    if not account:
        reasons.append("No initialized Alpaca paper account exists")
    elif account.activity_contract != "legacy-v1":
        reasons.append("Account is not on the legacy-v1 contract")
    if not stable:
        reasons.append("Broker state changed during the two-snapshot preflight")
    if account and account.broker_account_id != str(raw_account.get("id") or ""):
        reasons.append("Broker account identity differs from the local ledger")
    if positions:
        reasons.append("Open positions require a separate inventory migration review")
    if any(str(row.get("status") or "").lower() in NONTERMINAL_ORDER_STATUSES for row in orders):
        reasons.append("Nonterminal broker orders require a separate order review")
    if account and (account.unexplained_residual or account.reconciliation_required):
        reasons.append("Existing accounting residual or reconciliation review must be resolved first")
    try:
        baseline = alpaca_activity_v2.observed_baseline(
            activities,
            raw_account.get("cash"),
            {str(row["symbol"]): row.get("qty", row.get("quantity")) for row in positions},
            observed.isoformat(),
        )
    except (TypeError, ValueError, KeyError) as exc:
        baseline = None
        reasons.append(f"Current activity history is not valid v2 evidence: {exc}")
    return {
        "ready": not reasons,
        "paper_only": True,
        "live_authorized": False,
        "broker_write_calls": 0,
        "stable_snapshots": stable,
        "activity_count": len(activities),
        "order_count": len(orders),
        "position_count": len(positions),
        "baseline_journal_sha256": baseline["journal_sha256"] if baseline else None,
        "reasons": reasons,
    }, baseline


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="Apply only after a passing preflight")
    parser.add_argument("--confirm", default="",
                        help=f"Required with --apply: {CONFIRM}")
    args = parser.parse_args(argv)
    if settings.allow_live_trading or settings.active_paper_broker != "alpaca_paper":
        raise RuntimeError("Contract upgrade requires live-disabled Alpaca paper")
    gateway = active_paper_gateway()
    with SessionLocal() as db:
        account = db.query(StockPaperAccount).filter_by(broker="alpaca_paper").one_or_none()
        first = _snapshot(gateway)
        second = _snapshot(gateway)
        report, baseline = _report(account, first, second)
        if args.apply:
            if args.confirm != CONFIRM:
                raise RuntimeError(f"--apply requires --confirm {CONFIRM}")
            if not report["ready"]:
                raise RuntimeError("Preflight blocked: " + "; ".join(report["reasons"]))
            account.activity_contract = alpaca_activity_v2.VERSION
            account.activity_baseline = baseline
            account.costs_known = False
            account.accounting_verified = False
            account.reconciliation_required = True
            db.add(StockPaperLedgerEvent(
                account_id=account.id,
                event_type="activity_contract_upgrade",
                status="applied",
                actor="operator",
                reason="Explicit legacy-v1 to Alpaca activity v2 contract upgrade",
                payload={**report, "confirmation_sha256": hashlib.sha256(args.confirm.encode()).hexdigest()},
                created_at=datetime.now(timezone.utc),
            ))
            db.commit()
            report["applied"] = True
        else:
            report["applied"] = False
        print(json.dumps(report, sort_keys=True))
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
