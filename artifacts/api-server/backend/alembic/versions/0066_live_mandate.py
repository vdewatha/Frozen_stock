"""Standing two-person live mandate, its append-only events, and the pilot link.

Mandates and mandate events are immutable via triggers. ``live_pilot`` gains a
nullable ``mandate_id`` so a scheduler-minted window names its authority.
"""
from alembic import op
import sqlalchemy as sa


revision = "0066_live_mandate"
down_revision = "0065_loss_feedback_loop"
branch_labels = None
depends_on = None

IMMUTABLE_TABLES = ("live_mandates", "live_mandate_events")


def _create_table_if_missing(bind, table_name: str, *elements) -> None:
    if not sa.inspect(bind).has_table(table_name):
        op.create_table(table_name, *elements)


def _create_index_if_missing(bind, name: str, table_name: str, columns: list[str], unique: bool = False) -> None:
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes(table_name)}
    if name not in indexes:
        op.create_index(name, table_name, columns, unique=unique)


def _trigger_exists(bind, name: str) -> bool:
    if bind.dialect.name == "postgresql":
        return bool(bind.execute(sa.text("SELECT 1 FROM pg_trigger WHERE tgname = :name"), {"name": name}).first())
    if bind.dialect.name == "sqlite":
        return bool(bind.execute(sa.text(
            "SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = :name"
        ), {"name": name}).first())
    return False


def _created_at():
    return sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def upgrade() -> None:
    bind = op.get_bind()
    _create_table_if_missing(
        bind, "live_mandates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbols", sa.JSON(), nullable=False),
        sa.Column("max_notional", sa.Numeric(20, 8), nullable=False),
        sa.Column("max_order_notional", sa.Numeric(20, 8), nullable=False),
        sa.Column("allowed_order_types", sa.JSON(), nullable=False),
        sa.Column("max_pilot_days", sa.Integer(), nullable=False),
        sa.Column("renewal_lead_hours", sa.Integer(), nullable=False),
        sa.Column("observation_window_sessions", sa.Integer(), nullable=False),
        sa.Column("max_cumulative_loss", sa.Numeric(20, 8), nullable=False),
        sa.Column("max_drawdown", sa.Numeric(10, 6), nullable=False),
        sa.Column("rollback_target", sa.String(length=16), nullable=False),
        sa.Column("model_run_id", sa.String(length=64), nullable=False),
        sa.Column("launch_checklist", sa.JSON(), nullable=False),
        sa.Column("paper_expectations", sa.JSON(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("primary_approval_actor", sa.String(length=128), nullable=False),
        sa.Column("secondary_approval_actor", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("supersedes_mandate_id", sa.Integer(), sa.ForeignKey("live_mandates.id")),
        sa.Column("mandate_sha256", sa.String(length=64), nullable=False),
        _created_at(),
        sa.CheckConstraint("max_notional > 0", name="ck_live_mandate_max_notional"),
        sa.CheckConstraint(
            "max_order_notional > 0 AND max_order_notional <= max_notional",
            name="ck_live_mandate_order_notional",
        ),
        sa.CheckConstraint("max_cumulative_loss > 0", name="ck_live_mandate_loss_cap"),
        sa.CheckConstraint("max_drawdown > 0", name="ck_live_mandate_drawdown"),
        sa.CheckConstraint("max_pilot_days >= 1 AND max_pilot_days <= 30", name="ck_live_mandate_pilot_days"),
        sa.CheckConstraint(
            "observation_window_sessions >= 1 AND observation_window_sessions <= 20",
            name="ck_live_mandate_observation",
        ),
        sa.CheckConstraint("rollback_target IN ('paper', 'shadow')", name="ck_live_mandate_rollback"),
        sa.CheckConstraint("primary_approval_actor <> secondary_approval_actor", name="ck_live_mandate_two_person"),
        sa.CheckConstraint("expires_at > valid_from", name="ck_live_mandate_window"),
    )
    _create_index_if_missing(bind, "uq_live_mandates_sha256", "live_mandates", ["mandate_sha256"], unique=True)
    _create_index_if_missing(bind, "ix_live_mandates_expires_at", "live_mandates", ["expires_at"])
    _create_index_if_missing(bind, "ix_live_mandates_created_at", "live_mandates", ["created_at"])
    _create_index_if_missing(
        bind, "ix_live_mandates_supersedes_mandate_id", "live_mandates", ["supersedes_mandate_id"],
    )

    _create_table_if_missing(
        bind, "live_mandate_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("mandate_id", sa.Integer(), sa.ForeignKey("live_mandates.id"), nullable=False),
        sa.Column("action", sa.String(length=48), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("secondary_actor", sa.String(length=128)),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("event_sha256", sa.String(length=64), nullable=False),
        _created_at(),
    )
    _create_index_if_missing(
        bind, "uq_live_mandate_events_sha256", "live_mandate_events", ["event_sha256"], unique=True,
    )
    _create_index_if_missing(bind, "ix_live_mandate_events_mandate_id", "live_mandate_events", ["mandate_id"])
    _create_index_if_missing(bind, "ix_live_mandate_events_action", "live_mandate_events", ["action"])
    _create_index_if_missing(bind, "ix_live_mandate_events_created_at", "live_mandate_events", ["created_at"])

    pilot_columns = {column["name"] for column in sa.inspect(bind).get_columns("live_pilot")}
    if "mandate_id" not in pilot_columns:
        op.add_column("live_pilot", sa.Column("mandate_id", sa.Integer()))
    _create_index_if_missing(bind, "ix_live_pilot_mandate_id", "live_pilot", ["mandate_id"])

    if bind.dialect.name == "postgresql":
        op.execute(sa.text(
            """
            CREATE OR REPLACE FUNCTION reject_live_mandate_mutation()
            RETURNS trigger AS $$
            BEGIN
              RAISE EXCEPTION 'live mandate records are immutable';
            END;
            $$ LANGUAGE plpgsql
            """
        ))
        for table in IMMUTABLE_TABLES:
            name = f"{table}_immutable"
            if not _trigger_exists(bind, name):
                op.execute(sa.text(
                    f"CREATE TRIGGER {name} BEFORE UPDATE OR DELETE ON {table} "
                    "FOR EACH ROW EXECUTE FUNCTION reject_live_mandate_mutation()"
                ))
    elif bind.dialect.name == "sqlite":
        for table in IMMUTABLE_TABLES:
            for operation in ("update", "delete"):
                name = f"{table}_immutable_{operation}"
                if not _trigger_exists(bind, name):
                    op.execute(sa.text(
                        f"CREATE TRIGGER {name} BEFORE {operation.upper()} ON {table} "
                        "BEGIN SELECT RAISE(ABORT, 'live mandate records are immutable'); END"
                    ))


def downgrade() -> None:
    raise RuntimeError("0066 is forward-only; restore a verified backup to roll back")
