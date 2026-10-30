"""Records that let the paper learning loop run without an operator.

* ``StockPaperMandate`` is an immutable, admin-approved standing authorization
  from which the scheduler derives exact per-cycle paper-run approvals.
  Revocation and the per-cycle minting links are separate append-only rows so
  the mandate itself never changes.
* ``StockStrategyReadmission`` is the mutable re-admission state for one
  strategy suspended for edge decay (quarantine, backoff, retirement).
* ``NotificationDelivery`` is the external alert outbox (webhook, Telegram,
  email) with dedupe keys and retry bookkeeping.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, Text, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class StockPaperMandate(Base):
    """Immutable standing paper-only authorization approved by an admin."""
    __tablename__ = "stock_paper_mandates"
    __table_args__ = (
        CheckConstraint("environment = 'paper'", name="ck_stock_paper_mandate_environment"),
        CheckConstraint("paper_only = true", name="ck_stock_paper_mandate_paper_only"),
        CheckConstraint("live_authorized = false", name="ck_stock_paper_mandate_live_disabled"),
        CheckConstraint("max_sessions > 0", name="ck_stock_paper_mandate_max_sessions"),
        CheckConstraint("max_sessions_per_cycle > 0", name="ck_stock_paper_mandate_cycle_sessions"),
        Index("uq_stock_paper_mandates_sha256", "mandate_sha256", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    environment: Mapped[str] = mapped_column(String(16), nullable=False, default="paper")
    execution_provider: Mapped[str] = mapped_column(String(64), nullable=False)
    symbols: Mapped[list] = mapped_column(JSON, nullable=False)
    exposure_limits: Mapped[dict] = mapped_column(JSON, nullable=False)
    loss_limits: Mapped[dict] = mapped_column(JSON, nullable=False)
    schedule: Mapped[dict] = mapped_column(JSON, nullable=False)
    stop_conditions: Mapped[list] = mapped_column(JSON, nullable=False)
    stop_authority: Mapped[str] = mapped_column(String(64), nullable=False)
    pending_order_treatment: Mapped[str] = mapped_column(String(32), nullable=False)
    remaining_position_policies: Mapped[list] = mapped_column(JSON, nullable=False)
    max_sessions_per_cycle: Mapped[int] = mapped_column(Integer, nullable=False)
    max_sessions: Mapped[int] = mapped_column(Integer, nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    approving_actors: Mapped[list] = mapped_column(JSON, nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    mandate_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    paper_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    live_authorized: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True,
    )


class StockPaperMandateRevocation(Base):
    """Append-only revocation of exactly one mandate."""
    __tablename__ = "stock_paper_mandate_revocations"
    __table_args__ = (
        Index("uq_stock_paper_mandate_revocations_mandate", "mandate_id", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    mandate_id: Mapped[int] = mapped_column(ForeignKey("stock_paper_mandates.id"), nullable=False)
    revoked_by: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )


class StockPaperMandateApproval(Base):
    """Append-only link from a minted paper-run approval to its mandate."""
    __tablename__ = "stock_paper_mandate_approvals"
    __table_args__ = (
        Index("uq_stock_paper_mandate_approvals_approval", "approval_id", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    mandate_id: Mapped[int] = mapped_column(
        ForeignKey("stock_paper_mandates.id"), nullable=False, index=True,
    )
    approval_id: Mapped[int] = mapped_column(
        ForeignKey("stock_paper_run_approvals.id"), nullable=False,
    )
    cycle_id: Mapped[str] = mapped_column(
        ForeignKey("stock_learning_cycles.cycle_id"), nullable=False, index=True,
    )
    duration_sessions: Mapped[int] = mapped_column(Integer, nullable=False)
    fit: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )


class StockStrategyReadmission(Base):
    """Mutable re-admission state for one edge-decay-suspended strategy."""
    __tablename__ = "stock_strategy_readmissions"
    __table_args__ = (
        Index("uq_stock_strategy_readmissions_strategy", "strategy_id", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    suspended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)
    current_cycle_id: Mapped[Optional[str]] = mapped_column(String(64))
    successor_cycle_id: Mapped[Optional[str]] = mapped_column(String(64))
    last_reason: Mapped[Optional[str]] = mapped_column(Text)
    history: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
    )


class NotificationDelivery(Base):
    """External alert outbox row; one per channel and dedupe window."""
    __tablename__ = "notification_deliveries"
    __table_args__ = (
        Index("ix_notification_deliveries_channel_key", "channel", "dedupe_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    notification_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("notifications.id"), index=True,
    )
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="alert")
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    suppressed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
    )
