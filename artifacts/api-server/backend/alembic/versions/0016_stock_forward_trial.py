"""Durable controlled stock forward-paper trials."""
from alembic import op
import sqlalchemy as sa

revision = "0016_stock_forward_trial"
down_revision = "0015_merge_stock_branches"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table("stock_paper_trials",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("binding_id", sa.Integer(), sa.ForeignKey("stock_paper_model_bindings.id"), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="approved"),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("policy", sa.JSON(), nullable=False),
        sa.Column("lineage", sa.JSON(), nullable=False),
        sa.Column("blocked_reason", sa.Text()),
        sa.Column("pause_reason", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("stopped_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('approved','blocked','paused','running','stopped','completed')", name="ck_stock_trial_status"))
    op.create_index("ix_stock_paper_trials_status", "stock_paper_trials", ["status"])
    op.create_table("stock_paper_trial_decisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("trial_id", sa.String(36), sa.ForeignKey("stock_paper_trials.id"), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("bar_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decision_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("rejection_reason", sa.Text()),
        sa.Column("qualifying", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("lineage", sa.JSON(), nullable=False),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("stock_paper_orders.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("trial_id", "symbol", "bar_timestamp", name="uq_stock_trial_decision_bar"))
    op.create_index("ix_stock_paper_trial_decisions_trial_id", "stock_paper_trial_decisions", ["trial_id"])
    op.create_table("stock_paper_trial_metrics",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("trial_id", sa.String(36), sa.ForeignKey("stock_paper_trials.id"), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("classification", sa.String(24), nullable=False, server_default="accumulating"),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint("trial_id", "as_of", name="uq_stock_trial_metric_asof"))
    op.create_index("ix_stock_paper_trial_metrics_trial_id", "stock_paper_trial_metrics", ["trial_id"])

def downgrade() -> None:
    op.drop_table("stock_paper_trial_metrics")
    op.drop_table("stock_paper_trial_decisions")
    op.drop_table("stock_paper_trials")