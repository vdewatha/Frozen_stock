"""Durable forward-only research predictions; never execution authority."""
from alembic import op
import sqlalchemy as sa

revision = "0048_online_research"
down_revision = "0047_paper_venue_qualification"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "online_research_forecasts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("source_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("target_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("prediction", sa.JSON(), nullable=False),
        sa.Column("outcome", sa.JSON(), nullable=True),
        sa.UniqueConstraint("symbol", "version", "source_at", name="uq_online_research_source"),
    )
    op.create_index("ix_online_research_forecasts_symbol", "online_research_forecasts", ["symbol"])
    op.create_index("ix_online_research_forecasts_status", "online_research_forecasts", ["status"])


def downgrade():
    raise RuntimeError("0048 is forward-only; restore a verified backup to roll back")
