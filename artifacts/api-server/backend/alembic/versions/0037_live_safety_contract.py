"""Add the durable fail-closed live safety contract."""
from alembic import op
import sqlalchemy as sa


revision = "0037_live_safety_contract"
down_revision = "0036_learning_schedule_index"
branch_labels = None
depends_on = None

MODES = "('research', 'paper', 'shadow', 'canary-live', 'approved-live', 'emergency-stop')"


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "live_safety_state" not in tables:
        op.create_table(
            "live_safety_state",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("mode", sa.String(length=24), nullable=False, server_default="research"),
            sa.Column("approval_actor", sa.String(length=128)),
            sa.Column("approval_at", sa.DateTime(timezone=True)),
            sa.Column("secondary_approval_actor", sa.String(length=128)),
            sa.Column("secondary_approval_at", sa.DateTime(timezone=True)),
            sa.Column("gates", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("lineage", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("last_reason", sa.Text(), nullable=False, server_default="Live execution is not approved"),
            sa.Column("updated_by", sa.String(length=128), nullable=False, server_default="system"),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint("id = 1", name="ck_live_safety_state_singleton"),
            sa.CheckConstraint(f"mode IN {MODES}", name="ck_live_safety_mode"),
        )
        op.create_index("ix_live_safety_state_mode", "live_safety_state", ["mode"])
        live_state = sa.table(
            "live_safety_state",
            sa.column("id", sa.Integer()),
            sa.column("mode", sa.String(length=24)),
            sa.column("gates", sa.JSON()),
            sa.column("lineage", sa.JSON()),
            sa.column("last_reason", sa.Text()),
            sa.column("updated_by", sa.String(length=128)),
        )
        op.execute(
            live_state.insert().values(
                id=1,
                mode="research",
                gates={},
                lineage={},
                last_reason="Live execution is not approved",
                updated_by="system",
            )
        )

    if "live_safety_events" not in tables:
        op.create_table(
            "live_safety_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("from_mode", sa.String(length=24)),
            sa.Column("to_mode", sa.String(length=24), nullable=False),
            sa.Column("action", sa.String(length=32), nullable=False),
            sa.Column("actor", sa.String(length=128), nullable=False),
            sa.Column("approval_actor", sa.String(length=128)),
            sa.Column("secondary_approval_actor", sa.String(length=128)),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("gates", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("evidence", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("event_sha256", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint(
                f"from_mode IS NULL OR from_mode IN {MODES}",
                name="ck_live_safety_event_from_mode",
            ),
            sa.CheckConstraint(f"to_mode IN {MODES}", name="ck_live_safety_event_to_mode"),
            sa.UniqueConstraint("event_sha256", name="uq_live_safety_event_digest"),
        )
        op.create_index("ix_live_safety_events_created_at", "live_safety_events", ["created_at"])

    if bind.dialect.name == "sqlite":
        op.execute(
            "CREATE TRIGGER IF NOT EXISTS live_safety_events_immutable_update "
            "BEFORE UPDATE ON live_safety_events BEGIN "
            "SELECT RAISE(ABORT, 'live safety events are immutable'); END"
        )
        op.execute(
            "CREATE TRIGGER IF NOT EXISTS live_safety_events_immutable_delete "
            "BEFORE DELETE ON live_safety_events BEGIN "
            "SELECT RAISE(ABORT, 'live safety events are immutable'); END"
        )
    elif bind.dialect.name == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION enforce_live_safety_events_immutable()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'live safety events are immutable';
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        op.execute(
            """
            CREATE TRIGGER live_safety_events_immutable
            BEFORE UPDATE OR DELETE ON live_safety_events
            FOR EACH ROW EXECUTE FUNCTION enforce_live_safety_events_immutable();
            """
        )


def downgrade() -> None:
    raise RuntimeError("0037 is forward-only; restore a verified backup to roll back")