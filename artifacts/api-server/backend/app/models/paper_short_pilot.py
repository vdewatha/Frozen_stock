"""Exclusive-account development paper short pilot, separate from long accounting."""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, DateTime, JSON, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PaperShortPilot(Base):
    __tablename__ = "paper_short_pilot"
    __table_args__ = (CheckConstraint("id = 1", name="ck_short_pilot_singleton"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    account_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    symbols: Mapped[list] = mapped_column(JSON, nullable=False)
    max_order_notional: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    max_gross_notional: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    baseline_equity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    authorized_by: Mapped[str] = mapped_column(String(128), nullable=False)
    authorized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    automated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")


class PaperShortOrder(Base):
    __tablename__ = "paper_short_orders"
    __table_args__ = (
        # One unresolved broker order for the entire account, not just per symbol.
        UniqueConstraint("active_slot", name="uq_short_order_active_slot"),
        UniqueConstraint("broker_order_id", name="uq_short_order_broker_id"),
        CheckConstraint("quantity > 0 AND limit_price > 0", name="ck_short_order_positive"),
        CheckConstraint("filled_quantity >= 0 AND filled_quantity <= quantity", name="ck_short_order_filled"),
        CheckConstraint("intent IN ('open_short', 'cover_short')", name="ck_short_order_intent"),
        CheckConstraint("active_slot IS NULL OR active_slot = 1", name="ck_short_order_slot"),
    )
    client_order_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    intent: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(21, 9), nullable=False)
    limit_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    filled_quantity: Mapped[Decimal] = mapped_column(Numeric(21, 9), nullable=False, default=Decimal("0"))
    filled_avg_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    broker_order_id: Mapped[str | None] = mapped_column(String(96))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    active_slot: Mapped[int | None] = mapped_column()
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    broker_evidence: Mapped[dict | None] = mapped_column(JSON)
