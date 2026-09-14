"""Add immutable automated paper promotion decisions."""
from alembic import op
import sqlalchemy as sa


revision = "0032_automatic_paper_promotion"
down_revision = "0031_session_evidence_reports"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stock_paper_promotion_decisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("cycle_id", sa.String(length=64), sa.ForeignKey("stock_learning_cycles.cycle_id"), nullable=False),
        sa.Column("trial_id", sa.String(length=36), sa.ForeignKey("stock_paper_trials.id")),
        sa.Column("report_id", sa.Integer(), sa.ForeignKey("stock_paper_promotion_readiness_reports.id")),
        sa.Column("model_run_id", sa.String(length=64), sa.ForeignKey("stock_model_registry.run_id")),
        sa.Column("snapshot_id", sa.String(length=64), sa.ForeignKey("stock_dataset_snapshots.snapshot_id")),
        sa.Column("decision", sa.String(length=24), nullable=False),
        sa.Column("gates", sa.JSON(), nullable=False),
        sa.Column("lineage", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("source_job", sa.String(length=128), nullable=False),
        sa.Column("correlation_id", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("before_binding_id", sa.Integer()),
        sa.Column("before_model_run_id", sa.String(length=64)),
        sa.Column("after_binding_id", sa.Integer()),
        sa.Column("after_model_run_id", sa.String(length=64)),
        sa.Column("decision_sha256", sa.String(length=64), nullable=False),
        sa.Column("paper_only", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("live_authorized", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("paper_only = true", name="ck_stock_promotion_decision_paper_only"),
        sa.CheckConstraint("live_authorized = false", name="ck_stock_promotion_decision_live_disabled"),
        sa.UniqueConstraint("decision_sha256", name="uq_stock_promotion_decision_digest"),
    )
    op.create_index("ix_stock_paper_promotion_decisions_cycle_id", "stock_paper_promotion_decisions", ["cycle_id"])
    op.create_index("ix_stock_paper_promotion_decisions_trial_id", "stock_paper_promotion_decisions", ["trial_id"])
    op.create_index("ix_stock_paper_promotion_decisions_report_id", "stock_paper_promotion_decisions", ["report_id"])
    op.create_index("ix_stock_paper_promotion_decisions_model_run_id", "stock_paper_promotion_decisions", ["model_run_id"])
    op.create_index("ix_stock_paper_promotion_decisions_decision", "stock_paper_promotion_decisions", ["decision"])
    op.create_index("ix_stock_paper_promotion_decisions_created_at", "stock_paper_promotion_decisions", ["created_at"])


def downgrade() -> None:
    raise RuntimeError("0032 is forward-only; restore a verified backup to roll back")