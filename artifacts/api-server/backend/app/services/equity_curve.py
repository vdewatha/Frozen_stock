"""Account equity history for charts, across the whole account life.

The charts used the newest 365 raw snapshots. Snapshots are written every
1-3 minutes, so that covered about a day and a half (2026-11-09: the curve
started 11-08 although the account was initialized 09-11) and the axis
repeated one date a dozen times.

This keeps the last snapshot of each UTC day (written after the close, so it
is that session's end-of-day equity) for the full history, plus one point
per ``recent_bucket_minutes`` for the last ``recent_hours``. Grouping the
daily points happens in SQL, so the full snapshot table is never loaded.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import StockPaperEquitySnapshot

UTC = timezone.utc


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def equity_curve_points(
    db: Session,
    account_id: int,
    *,
    now: datetime | None = None,
    recent_hours: int = 48,
    recent_bucket_minutes: int = 15,
) -> list[tuple[datetime, Decimal]]:
    """``[(observed_at, equity), ...]`` oldest first, one series without duplicates."""
    now = _utc(now or datetime.now(UTC))
    cutoff = now - timedelta(hours=recent_hours)
    snapshot = StockPaperEquitySnapshot
    day = func.date(snapshot.observed_at)
    last_per_day = (
        select(func.max(snapshot.observed_at))
        .where(snapshot.account_id == account_id, snapshot.observed_at < cutoff)
        .group_by(day)
    )
    daily = db.execute(
        select(snapshot.observed_at, snapshot.equity)
        .where(snapshot.account_id == account_id, snapshot.observed_at.in_(last_per_day))
        .order_by(snapshot.observed_at, snapshot.id)
    ).all()
    recent = db.execute(
        select(snapshot.observed_at, snapshot.equity)
        .where(snapshot.account_id == account_id, snapshot.observed_at >= cutoff)
        .order_by(snapshot.observed_at, snapshot.id)
    ).all()

    points: dict[object, tuple[datetime, Decimal]] = {}
    for observed_at, equity in daily:
        if equity is not None:
            points[("day", _utc(observed_at).date())] = (_utc(observed_at), equity)
    bucket_seconds = max(60, recent_bucket_minutes * 60)
    for observed_at, equity in recent:
        if equity is not None:
            stamp = _utc(observed_at)
            # The last snapshot in each bucket wins; the newest is always kept.
            points[("recent", int(stamp.timestamp()) // bucket_seconds)] = (stamp, equity)
    return sorted(points.values(), key=lambda point: point[0])
