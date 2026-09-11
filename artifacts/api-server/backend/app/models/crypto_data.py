"""Immutable, venue-scoped crypto observations separate from legacy equities."""
from sqlalchemy import Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class CryptoCandle(Base):
    __tablename__ = "crypto_candles"
    __table_args__ = (UniqueConstraint("instrument_id", "timeframe", "opened_at", name="uq_crypto_candle_interval"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument_id: Mapped[str] = mapped_column(String(80))
    timeframe: Mapped[str] = mapped_column(String(8))
    opened_at: Mapped[str] = mapped_column(String(40))
    observed_at: Mapped[str] = mapped_column(String(40))
    open: Mapped[float] = mapped_column(Numeric(30, 12))
    high: Mapped[float] = mapped_column(Numeric(30, 12))
    low: Mapped[float] = mapped_column(Numeric(30, 12))
    close: Mapped[float] = mapped_column(Numeric(30, 12))
    volume: Mapped[float] = mapped_column(Numeric(30, 12))
    content_sha256: Mapped[str] = mapped_column(String(64))


class CollectionRun(Base):
    __tablename__ = "crypto_collection_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument_id: Mapped[str] = mapped_column(String(80))
    timeframe: Mapped[str] = mapped_column(String(8))
    observed_at: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(16))
    fetched_count: Mapped[int] = mapped_column(Integer)
    inserted_count: Mapped[int] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
