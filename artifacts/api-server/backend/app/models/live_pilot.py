"""Durable controls and evidence for the narrowly scoped live pilot."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import Boolean, DateTime, JSON, Numeric, String, Text, CheckConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class LivePilot(Base):
    __tablename__ = "live_pilot"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_live_pilot_singleton"),
        CheckConstraint("max_notional > 0", name="ck_live_pilot_max_notional_positive"),
        CheckConstraint("max_order_notional > 0", name="ck_live_pilot_max_order_notional_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="inactive", index=True)
    symbols: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    max_notional: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, default=Decimal("10000"))
    max_order_notional: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, default=Decimal("1000"))
    allowed_order_types: Mapped[list] = mapped_column(JSON, nullable=False, default=lambda: ["limit"])
    time_in_force: Mapped[str] = mapped_column(String(16), nullable=False, default="day")
    session_policy: Mapped[str] = mapped_column(String(32), nullable=False, default="regular")
    starts_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    observation_window_sessions: Mapped[int] = mapped_column(nullable=False, default=5)
    rollback_target: Mapped[str] = mapped_column(String(16), nullable=False, default="paper")
    model_run_id: Mapped[Optional[str]] = mapped_column(String(64))
    paper_expectations: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    launch_checklist: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    primary_approval_actor: Mapped[Optional[str]] = mapped_column(String(128))
    secondary_approval_actor: Mapped[Optional[str]] = mapped_column(String(128))
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    stopped_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    stopped_reason: Mapped[Optional[str]] = mapped_column(Text)
    latest_review: Mapped[Optional[dict]] = mapped_column(JSON)
    updated_by: Mapped[str] = mapped_column(String(128), nullable=False, default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class LivePilotEvent(Base):
    __tablename__ = "live_pilot_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    action: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    secondary_actor: Mapped[Optional[str]] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    event_sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)