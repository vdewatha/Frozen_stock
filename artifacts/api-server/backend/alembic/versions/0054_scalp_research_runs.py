"""Add an isolated paper-only intraday scalp research lane."""
from alembic import op
import sqlalchemy as sa

revision = "0059_scalp_research_runs"
down_revision = "0058_market_price_provider_key"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "scalp_research_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("requested_by", sa.String(length=128), nullable=False),
        sa.Column("symbols", sa.JSON(), nullable=False),
        sa.Column("timeframe", sa.String(length=8), nullable=False, server_default="1m"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("eligible_for_trading", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("data_snapshot", sa.JSON(), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("assumptions", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("completed_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("run_id", name="uq_scalp_research_run_id"),
        sa.CheckConstraint("status IN ('queued', 'running', 'completed', 'failed', 'blocked')", name="ck_scalp_research_run_status"),
        sa.CheckConstraint("eligible_for_trading = false", name="ck_scalp_research_run_ineligible"),
    )
    op.create_index("ix_scalp_research_runs_status", "scalp_research_runs", ["status"])
    op.create_index("ix_scalp_research_runs_created_at", "scalp_research_runs", ["created_at"])


def downgrade():
    raise RuntimeError("0054 is forward-only; restore a verified backup to roll back")
