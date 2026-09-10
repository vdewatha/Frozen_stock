"""Durable isolated crypto candles and collection audit."""
from alembic import op
import sqlalchemy as sa

revision = "0005_crypto_observations"
down_revision = "0004_research_model_registry"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("crypto_candles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("instrument_id", sa.String(80), nullable=False),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("opened_at", sa.String(40), nullable=False),
        sa.Column("observed_at", sa.String(40), nullable=False),
        *[sa.Column(name, sa.Numeric(30, 12), nullable=False) for name in ("open", "high", "low", "close", "volume")],
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.UniqueConstraint("instrument_id", "timeframe", "opened_at", name="uq_crypto_candle_interval"))
    op.create_table("crypto_collection_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("instrument_id", sa.String(80), nullable=False),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("observed_at", sa.String(40), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("fetched_count", sa.Integer(), nullable=False),
        sa.Column("inserted_count", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(80), nullable=True))


def downgrade():
    raise RuntimeError("0005 is forward-only; restore a verified backup to roll back")
