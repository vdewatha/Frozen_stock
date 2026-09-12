"""Durable stock drift, performance, execution, risk, and health monitoring."""
from alembic import op
import sqlalchemy as sa

revision = "0024_stock_continuous_monitoring"
down_revision = "0023_stock_model_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stock_monitoring_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("monitor_key", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("checks", sa.JSON(), nullable=False),
        sa.Column("actions", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_stock_monitoring_snapshots_monitor_key", "stock_monitoring_snapshots", ["monitor_key"])
    op.create_index("ix_stock_monitoring_snapshots_status", "stock_monitoring_snapshots", ["status"])
    op.create_index("ix_stock_monitoring_snapshots_generated_at", "stock_monitoring_snapshots", ["generated_at"])

    op.create_table(
        "stock_monitoring_breaches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("breach_key", sa.String(length=128), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("metric", sa.String(length=96), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("severity", sa.String(length=24), nullable=False),
        sa.Column("observed_value", sa.JSON()),
        sa.Column("threshold", sa.JSON()),
        sa.Column("consecutive_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_observed_at", sa.DateTime(timezone=True)),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('unknown', 'observed', 'persistent', 'cleared')",
            name="ck_stock_monitoring_breach_status",
        ),
        sa.UniqueConstraint("breach_key", name="uq_stock_monitoring_breach_key"),
    )
    op.create_index("ix_stock_monitoring_breaches_category", "stock_monitoring_breaches", ["category"])
    op.create_index("ix_stock_monitoring_breaches_metric", "stock_monitoring_breaches", ["metric"])
    op.create_index("ix_stock_monitoring_breaches_status", "stock_monitoring_breaches", ["status"])


def downgrade() -> None:
    op.drop_index("ix_stock_monitoring_breaches_status", table_name="stock_monitoring_breaches")
    op.drop_index("ix_stock_monitoring_breaches_metric", table_name="stock_monitoring_breaches")
    op.drop_index("ix_stock_monitoring_breaches_category", table_name="stock_monitoring_breaches")
    op.drop_table("stock_monitoring_breaches")
    op.drop_index("ix_stock_monitoring_snapshots_generated_at", table_name="stock_monitoring_snapshots")
    op.drop_index("ix_stock_monitoring_snapshots_status", table_name="stock_monitoring_snapshots")
    op.drop_index("ix_stock_monitoring_snapshots_monitor_key", table_name="stock_monitoring_snapshots")
    op.drop_table("stock_monitoring_snapshots")