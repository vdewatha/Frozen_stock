"""Append-only paper promotion-readiness evidence."""
from alembic import op
import sqlalchemy as sa

revision = "0022_promotion_readiness_report"
down_revision = "0021_trial_exit_order_link"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stock_paper_promotion_readiness_reports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("trial_id", sa.String(36), sa.ForeignKey("stock_paper_trials.id"), nullable=False),
        sa.Column("source_metric_id", sa.Integer(), sa.ForeignKey("stock_paper_trial_metrics.id"), nullable=True),
        sa.Column("report_hash", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("gates", sa.JSON(), nullable=False),
        sa.Column("lineage", sa.JSON(), nullable=False),
        sa.Column("policy", sa.JSON(), nullable=False),
        sa.Column("paper_only", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("live_authorized", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("paper_only = true", name="ck_stock_readiness_paper_only"),
        sa.CheckConstraint("live_authorized = false", name="ck_stock_readiness_live_disabled"),
        sa.UniqueConstraint("report_hash", name="uq_stock_readiness_report_hash"),
    )
    op.create_index(
        "ix_stock_paper_promotion_readiness_reports_trial_id",
        "stock_paper_promotion_readiness_reports",
        ["trial_id"],
    )
    op.create_index(
        "ix_stock_paper_promotion_readiness_reports_source_metric_id",
        "stock_paper_promotion_readiness_reports",
        ["source_metric_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_stock_paper_promotion_readiness_reports_source_metric_id",
                  table_name="stock_paper_promotion_readiness_reports")
    op.drop_index("ix_stock_paper_promotion_readiness_reports_trial_id",
                  table_name="stock_paper_promotion_readiness_reports")
    op.drop_table("stock_paper_promotion_readiness_reports")