"""Research-only shadow decisions; deliberately no execution relationship."""
from datetime import datetime
from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, ForeignKey, JSON, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class ShadowModelBinding(Base):
    __tablename__ = "shadow_model_bindings"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("research_model_runs.run_id"), nullable=False)
    spec_sha256: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    instrument: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe_minutes: Mapped[int] = mapped_column(nullable=False)
    spec: Mapped[dict] = mapped_column(JSON, nullable=False)
    actor: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ShadowDecision(Base):
    __tablename__ = "shadow_decisions"
    __table_args__ = (UniqueConstraint("binding_id", "bar_close", name="uq_shadow_binding_bar"),
                      CheckConstraint("eligible_for_qualification = false", name="ck_shadow_research_only"))
    id: Mapped[int] = mapped_column(primary_key=True)
    binding_id: Mapped[int] = mapped_column(ForeignKey("shadow_model_bindings.id"), nullable=False)
    bar_close: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    data_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    spec_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    probability: Mapped[float] = mapped_column(Float, nullable=False)
    reference_price: Mapped[float] = mapped_column(Float, nullable=False)
    intended_action: Mapped[str] = mapped_column(String(24), nullable=False)
    backfilled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    eligible_for_qualification: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    latency_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    outcome: Mapped[dict | None] = mapped_column(JSON)


class ShadowRunAudit(Base):
    __tablename__ = "shadow_run_audits"
    id: Mapped[int] = mapped_column(primary_key=True)
    binding_id: Mapped[int] = mapped_column(ForeignKey("shadow_model_bindings.id"), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    reason: Mapped[str] = mapped_column(String(120), nullable=False)
