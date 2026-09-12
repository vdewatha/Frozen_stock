"""Durable stock paper rollback, cancellation, cooldown, and watchdog state."""
from alembic import op
import sqlalchemy as sa

revision = "0025_stock_paper_recovery"
down_revision = "0024_stock_continuous_monitoring"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stock_paper_recovery_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="armed"),
        sa.Column("flatten_policy", sa.String(length=16), nullable=False, server_default="none"),
        sa.Column("cooldown_until", sa.DateTime(timezone=True)),
        sa.Column("pause_reason", sa.Text()),
        sa.Column("last_known_good_model_run_id", sa.String(length=64)),
        sa.Column("last_known_good_binding_id", sa.Integer()),
        sa.Column("last_monitor_heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("last_watchdog_heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("last_revalidation_at", sa.DateTime(timezone=True)),
        sa.Column("updated_by", sa.String(length=128), nullable=False, server_default="system"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("id = 1", name="ck_stock_paper_recovery_state_singleton"),
        sa.CheckConstraint(
            "status IN ('armed', 'paused', 'cooldown', 'revalidation_required', 'resumable')",
            name="ck_stock_paper_recovery_status",
        ),
        sa.CheckConstraint(
            "flatten_policy IN ('none', 'positions')",
            name="ck_stock_paper_recovery_flatten_policy",
        ),
    )
    op.create_index("ix_stock_paper_recovery_state_status", "stock_paper_recovery_state", ["status"])

    op.create_table(
        "stock_paper_recovery_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("action", sa.String(length=48), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("event_sha256", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("event_sha256", name="uq_stock_paper_recovery_event_digest"),
    )
    op.create_index("ix_stock_paper_recovery_events_action", "stock_paper_recovery_events", ["action"])
    op.create_index("ix_stock_paper_recovery_events_status", "stock_paper_recovery_events", ["status"])
    op.create_index("ix_stock_paper_recovery_events_created_at", "stock_paper_recovery_events", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_stock_paper_recovery_events_created_at", table_name="stock_paper_recovery_events")
    op.drop_index("ix_stock_paper_recovery_events_status", table_name="stock_paper_recovery_events")
    op.drop_index("ix_stock_paper_recovery_events_action", table_name="stock_paper_recovery_events")
    op.drop_table("stock_paper_recovery_events")
    op.drop_index("ix_stock_paper_recovery_state_status", table_name="stock_paper_recovery_state")
    op.drop_table("stock_paper_recovery_state")