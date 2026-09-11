"""Paper-only execution state. These tables never authorize live trading."""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, BigInteger, String, UniqueConstraint, CheckConstraint, JSON, func
from sqlalchemy.types import TypeDecorator
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ExactAmount(TypeDecorator):
    """Eight-decimal fixed point; avoids SQLite REAL conversion entirely."""
    impl = BigInteger
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        number = Decimal(str(value)) * 100000000
        if not number.is_finite() or number != number.to_integral_value() or abs(number) >= 2**63:
            raise ValueError("Amount is outside exact eight-decimal ledger precision")
        return int(number)

    def process_result_value(self, value, dialect):
        return Decimal(value) / 100000000 if value is not None else None


class PaperExecutionAccount(Base):
    __tablename__ = "paper_execution_accounts"
    __table_args__ = (CheckConstraint("id = 1", name="ck_paper_account_singleton"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    cash: Mapped[Decimal] = mapped_column(ExactAmount())
    starting_cash: Mapped[Decimal] = mapped_column(ExactAmount())
    reserved_cash: Mapped[Decimal] = mapped_column(ExactAmount(), default=0)
    quantity: Mapped[Decimal] = mapped_column(ExactAmount(), default=0)
    reserved_quantity: Mapped[Decimal] = mapped_column(ExactAmount(), default=0)
    kill_switch: Mapped[bool] = mapped_column(Boolean, default=True)


class PaperTrialApproval(Base):
    __tablename__ = "paper_trial_approvals"
    id: Mapped[int] = mapped_column(primary_key=True)
    binding_id: Mapped[int] = mapped_column(ForeignKey("shadow_model_bindings.id"))
    model_run_id: Mapped[str] = mapped_column(String(64))
    binding_hash: Mapped[str] = mapped_column(String(64))
    policy_hash: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(128))
    max_notional: Mapped[Decimal] = mapped_column(ExactAmount())
    max_exposure: Mapped[Decimal] = mapped_column(ExactAmount())
    fee_rate: Mapped[Decimal] = mapped_column(ExactAmount())
    nonqualifying: Mapped[bool] = mapped_column(Boolean, default=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PaperOrderIntent(Base):
    __tablename__ = "paper_order_intents"
    __table_args__ = (UniqueConstraint("decision_id", "side", name="uq_paper_decision_side"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    client_order_id: Mapped[str] = mapped_column(String(128), unique=True)
    decision_id: Mapped[int] = mapped_column(ForeignKey("shadow_decisions.id"))
    approval_id: Mapped[int] = mapped_column(ForeignKey("paper_trial_approvals.id"))
    side: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[Decimal] = mapped_column(ExactAmount())
    limit_price: Mapped[Decimal] = mapped_column(ExactAmount())
    reserved_cash: Mapped[Decimal] = mapped_column(ExactAmount(), default=0)
    reserved_quantity: Mapped[Decimal] = mapped_column(ExactAmount(), default=0)
    filled_quantity: Mapped[Decimal] = mapped_column(ExactAmount(), default=0)
    status: Mapped[str] = mapped_column(String(24), default="approved")
    provider_order_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    provider_context: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PaperExecutionFill(Base):
    __tablename__ = "paper_execution_fills"
    id: Mapped[int] = mapped_column(primary_key=True)
    provider_fill_id: Mapped[str] = mapped_column(String(128), unique=True)
    intent_id: Mapped[int] = mapped_column(ForeignKey("paper_order_intents.id"))
    quantity: Mapped[Decimal] = mapped_column(ExactAmount())
    price: Mapped[Decimal] = mapped_column(ExactAmount())
    fee: Mapped[Decimal] = mapped_column(ExactAmount())
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PaperExternalFill(Base):
    """Observed provider exits, never fabricated model decisions or approvals."""
    __tablename__ = "paper_external_fills"
    id: Mapped[int] = mapped_column(primary_key=True)
    entry_intent_id: Mapped[int] = mapped_column(ForeignKey("paper_order_intents.id"))
    provider_order_id: Mapped[str] = mapped_column(String(128))
    snapshot_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    quantity: Mapped[Decimal] = mapped_column(ExactAmount())
    cost: Mapped[Decimal] = mapped_column(ExactAmount())
    fee: Mapped[Decimal] = mapped_column(ExactAmount())
    reason: Mapped[str] = mapped_column(String(32))
    observed_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
