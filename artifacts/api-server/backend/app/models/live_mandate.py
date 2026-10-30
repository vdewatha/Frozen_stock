"""Standing two-person live mandate and its append-only event history.

A ``LiveMandate`` is an immutable envelope approved by two distinct admin
identities.  The scheduler may mint, renew and promote bounded live-pilot
windows only inside the newest valid mandate.  Amendments create a new row that
supersedes the prior one; revocation, suspension, reinstatement, renewal
evaluations and lapses are append-only ``LiveMandateEvent`` rows, so a mandate
itself never changes after approval.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class LiveMandate(Base):
    __tablename__ = "live_mandates"
    __table_args__ = (
        CheckConstraint("max_notional > 0", name="ck_live_mandate_max_notional"),
        CheckConstraint(
            "max_order_notional > 0 AND max_order_notional <= max_notional",
            name="ck_live_mandate_order_notional",
        ),
        CheckConstraint("max_cumulative_loss > 0", name="ck_live_mandate_loss_cap"),
        CheckConstraint("max_drawdown > 0", name="ck_live_mandate_drawdown"),
        CheckConstraint("max_pilot_days >= 1 AND max_pilot_days <= 30", name="ck_live_mandate_pilot_days"),
        CheckConstraint(
            "observation_window_sessions >= 1 AND observation_window_sessions <= 20",
            name="ck_live_mandate_observation",
        ),
        CheckConstraint("rollback_target IN ('paper', 'shadow')", name="ck_live_mandate_rollback"),
        CheckConstraint("primary_approval_actor <> secondary_approval_actor", name="ck_live_mandate_two_person"),
        CheckConstraint("expires_at > valid_from", name="ck_live_mandate_window"),
        Index("uq_live_mandates_sha256", "mandate_sha256", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    symbols: Mapped[list] = mapped_column(JSON, nullable=False)
    max_notional: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    max_order_notional: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    allowed_order_types: Mapped[list] = mapped_column(JSON, nullable=False)
    max_pilot_days: Mapped[int] = mapped_column(Integer, nullable=False)
    renewal_lead_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    observation_window_sessions: Mapped[int] = mapped_column(Integer, nullable=False)
    max_cumulative_loss: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    max_drawdown: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False)
    rollback_target: Mapped[str] = mapped_column(String(16), nullable=False)
    model_run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    launch_checklist: Mapped[dict] = mapped_column(JSON, nullable=False)
    paper_expectations: Mapped[dict] = mapped_column(JSON, nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    primary_approval_actor: Mapped[str] = mapped_column(String(128), nullable=False)
    secondary_approval_actor: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    supersedes_mandate_id: Mapped[Optional[int]] = mapped_column(ForeignKey("live_mandates.id"), index=True)
    mandate_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True,
    )


class LiveMandateEvent(Base):
    __tablename__ = "live_mandate_events"
    __table_args__ = (
        Index("uq_live_mandate_events_sha256", "event_sha256", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    mandate_id: Mapped[int] = mapped_column(ForeignKey("live_mandates.id"), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    secondary_actor: Mapped[Optional[str]] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    event_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True,
    )
