from datetime import datetime

from sqlalchemy import JSON, DateTime, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class OnlineResearchForecast(Base):
    __tablename__ = "online_research_forecasts"
    __table_args__ = (UniqueConstraint("symbol", "version", "source_at", name="uq_online_research_source"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    version: Mapped[str] = mapped_column(String(32))
    source_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    target_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), index=True)
    prediction: Mapped[dict] = mapped_column(JSON)
    outcome: Mapped[dict | None] = mapped_column(JSON, nullable=True)
