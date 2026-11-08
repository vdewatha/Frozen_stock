"""Are dividends being booked in the paper account?  (DIV entries in the Alpaca activities feed.)

The slow book the feasibility report plans (step 2) holds ETFs for months, so
its paper return is only comparable with a replay if the paper account books
the distributions the replay assumes.  This reads the Alpaca paper account's
activities feed for ``activity_types=DIV`` (``GET /v2/account/activities``,
the same paginated client the ledger reconciles with) and reports whether any
dividend has been booked, plus the DIV rows already imported into the local
activity table.

Fail safe: no credentials, a broker error or an empty feed is reported as
``unavailable`` / ``none_found``, never raised; nothing is written to the
ledger, the account or any strategy.  A weekly job records the result as a
``dividend_check`` audit row, and the yardstick and learning-scoreboard
endpoints show the latest one.  Paper only.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)
UTC = timezone.utc
EVENT = "dividend_check"
ACTION = "check"
LOOKBACK_DAYS = 400


def _float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fetch_dividends(client=None, *, now: Optional[datetime] = None, lookback_days: int = LOOKBACK_DAYS) -> list[dict]:
    """DIV activities of the last ``lookback_days`` from the Alpaca paper feed (raises when unreachable)."""
    from app.services.stock_paper_ledger import AlpacaPaperClient

    client = client or AlpacaPaperClient()
    now = now or datetime.now(UTC)
    after = (now - timedelta(days=lookback_days)).date().isoformat()
    return client._activity_pages({"activity_types": "DIV", "page_size": "100", "direction": "asc", "after": after})


def summarise(rows: list[dict]) -> dict:
    rows = [row for row in rows if str(row.get("activity_type") or "DIV").upper().startswith("DIV")]
    symbols: dict[str, dict] = {}
    total = 0.0
    dates = []
    for row in rows:
        symbol = str(row.get("symbol") or "?").upper()
        amount = _float(row.get("net_amount")) or 0.0
        total += amount
        day = str(row.get("date") or row.get("transaction_time") or "")[:10]
        if day:
            dates.append(day)
        entry = symbols.setdefault(symbol, {"entries": 0, "net_amount": 0.0, "last": None})
        entry["entries"] += 1
        entry["net_amount"] = round(entry["net_amount"] + amount, 4)
        entry["last"] = max(filter(None, [entry["last"], day])) if day else entry["last"]
    return {"entries": len(rows), "net_amount": round(total, 4), "first": min(dates) if dates else None,
            "last": max(dates) if dates else None, "by_symbol": symbols}


def _local_rows(db: Session) -> int:
    from app.models.stock_paper import StockPaperBrokerActivity

    return db.query(StockPaperBrokerActivity).filter(StockPaperBrokerActivity.activity_type.like("DIV%")).count()


def check_dividends(db: Session, *, now: Optional[datetime] = None, fetch: Optional[Callable[[], list[dict]]] = None) -> dict:
    """Whether DIV entries appear in the paper feed; ``unavailable`` when the feed cannot be read."""
    now = now or datetime.now(UTC)
    try:
        local = _local_rows(db)
    except Exception as exc:
        db.rollback()
        logger.warning("dividend check: local activity rows unavailable: %s", exc)
        local = None
    try:
        rows = (fetch or (lambda: fetch_dividends(now=now)))()
    except Exception as exc:  # no credentials, broker down: report, never raise
        return {"status": "unavailable", "booked": None, "checked_at": now.isoformat(),
                "reason": f"{exc.__class__.__name__}: {exc}"[:200], "local_div_rows": local,
                "lookback_days": LOOKBACK_DAYS, "paper_only": True}
    summary = summarise(list(rows or []))
    booked = summary["entries"] > 0
    return {"status": "booked" if booked else "none_found", "booked": booked, "checked_at": now.isoformat(),
            "lookback_days": LOOKBACK_DAYS, "local_div_rows": local, **summary,
            "reason": (f"{summary['entries']} DIV entries, ${summary['net_amount']:.2f} net, last {summary['last']}"
                       if booked else "no DIV entry in the paper activities feed for the lookback window"),
            "paper_only": True}


def record_dividend_check(db: Session, *, now: Optional[datetime] = None, fetch=None) -> dict:
    from app.services.audit import write_audit_log

    result = check_dividends(db, now=now, fetch=fetch)
    write_audit_log(db, event_type=EVENT, entity_type="stock_paper_account", action=ACTION, status=result["status"],
                    message=f"dividend check: {result['status']} ({result['reason']})", payload=result)
    db.commit()
    return result


def dividend_status(db: Session) -> dict:
    """The latest recorded check, or ``no_data`` before the first one."""
    from app.models import AuditLog

    row = db.query(AuditLog).filter(AuditLog.event_type == EVENT, AuditLog.action == ACTION).order_by(
        AuditLog.id.desc()).first()
    if row is None:
        return {"status": "no_data", "booked": None, "reason": "the weekly dividend check has not run yet"}
    return dict(row.payload or {})
