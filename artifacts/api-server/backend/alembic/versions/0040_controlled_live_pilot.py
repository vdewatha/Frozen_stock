"""Add the durable controlled live-pilot boundary."""
from alembic import op
import sqlalchemy as sa


revision = "0040_controlled_live_pilot"
down_revision = "0040_live_recovery_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "live_pilot",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="inactive"),
        sa.Column("symbols", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("max_notional", sa.Numeric(20, 8), nullable=False, server_default="10000"),
        sa.Column("max_order_notional", sa.Numeric(20, 8), nullable=False, server_default="1000"),
        sa.Column("allowed_order_types", sa.JSON(), nullable=False, server_default=sa.text("'[\"limit\"]'")),
        sa.Column("time_in_force", sa.String(length=16), nullable=False, server_default="day"),
        sa.Column("session_policy", sa.String(length=32), nullable=False, server_default="regular"),
        sa.Column("starts_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("observation_window_sessions", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("rollback_target", sa.String(length=16), nullable=False, server_default="paper"),
        sa.Column("model_run_id", sa.String(length=64)),
        sa.Column("paper_expectations", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("launch_checklist", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("primary_approval_actor", sa.String(length=128)),
        sa.Column("secondary_approval_actor", sa.String(length=128)),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("stopped_at", sa.DateTime(timezone=True)),
        sa.Column("stopped_reason", sa.Text()),
        sa.Column("latest_review", sa.JSON()),
        sa.Column("updated_by", sa.String(length=128), nullable=False, server_default="system"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("id = 1", name="ck_live_pilot_singleton"),
        sa.CheckConstraint("max_notional > 0", name="ck_live_pilot_max_notional_positive"),
        sa.CheckConstraint("max_order_notional > 0", name="ck_live_pilot_max_order_notional_positive"),
    )
    op.create_index("ix_live_pilot_status", "live_pilot", ["status"])
    op.execute(
        sa.text(
            "INSERT INTO live_pilot "
            "(id, status, symbols, max_notional, max_order_notional, allowed_order_types, "
            "time_in_force, session_policy, observation_window_sessions, rollback_target, "
            "paper_expectations, launch_checklist, updated_by) "
            "VALUES (1, 'inactive', '[]', 10000, 1000, '[\"limit\"]', 'day', 'regular', 5, 'paper', '{}', '{}', 'system')"
        )
    )
    op.create_table(
        "live_pilot_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("action", sa.String(length=48), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("secondary_actor", sa.String(length=128)),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("event_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("event_sha256", name="uq_live_pilot_event_digest"),
    )
    op.create_index("ix_live_pilot_events_action", "live_pilot_events", ["action"])
    op.create_index("ix_live_pilot_events_status", "live_pilot_events", ["status"])
    op.create_index("ix_live_pilot_events_created_at", "live_pilot_events", ["created_at"])


def downgrade() -> None:
    raise RuntimeError("0040 is forward-only; restore a verified backup to roll back")