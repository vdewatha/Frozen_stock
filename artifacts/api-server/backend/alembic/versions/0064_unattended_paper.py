"""Standing paper mandates, strategy re-admission state, and alert delivery.

Mandates, their revocations, and their minted-approval links are append-only
(immutable via triggers). Re-admission state and the delivery outbox are
mutable operational rows.
"""
from alembic import op
import sqlalchemy as sa


revision = "0064_unattended_paper"
down_revision = "0063_stock_paper_bracket_exits"
branch_labels = None
depends_on = None

IMMUTABLE_TABLES = (
    "stock_paper_mandates",
    "stock_paper_mandate_revocations",
    "stock_paper_mandate_approvals",
)


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
        bind, "stock_paper_mandates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("environment", sa.String(length=16), nullable=False, server_default="paper"),
        sa.Column("execution_provider", sa.String(length=64), nullable=False),
        sa.Column("symbols", sa.JSON(), nullable=False),
        sa.Column("exposure_limits", sa.JSON(), nullable=False),
        sa.Column("loss_limits", sa.JSON(), nullable=False),
        sa.Column("schedule", sa.JSON(), nullable=False),
        sa.Column("stop_conditions", sa.JSON(), nullable=False),
        sa.Column("stop_authority", sa.String(length=64), nullable=False),
        sa.Column("pending_order_treatment", sa.String(length=32), nullable=False),
        sa.Column("remaining_position_policies", sa.JSON(), nullable=False),
        sa.Column("max_sessions_per_cycle", sa.Integer(), nullable=False),
        sa.Column("max_sessions", sa.Integer(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approving_actors", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("mandate_sha256", sa.String(length=64), nullable=False),
        sa.Column("paper_only", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("live_authorized", sa.Boolean(), nullable=False, server_default=sa.false()),
        _created_at(),
        sa.CheckConstraint("environment = 'paper'", name="ck_stock_paper_mandate_environment"),
        sa.CheckConstraint("paper_only = true", name="ck_stock_paper_mandate_paper_only"),
        sa.CheckConstraint("live_authorized = false", name="ck_stock_paper_mandate_live_disabled"),
        sa.CheckConstraint("max_sessions > 0", name="ck_stock_paper_mandate_max_sessions"),
        sa.CheckConstraint("max_sessions_per_cycle > 0", name="ck_stock_paper_mandate_cycle_sessions"),
    )
    _create_index_if_missing(bind, "uq_stock_paper_mandates_sha256", "stock_paper_mandates", ["mandate_sha256"], unique=True)
    _create_index_if_missing(bind, "ix_stock_paper_mandates_expires_at", "stock_paper_mandates", ["expires_at"])
    _create_index_if_missing(bind, "ix_stock_paper_mandates_created_at", "stock_paper_mandates", ["created_at"])

    _create_table_if_missing(
        bind, "stock_paper_mandate_revocations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("mandate_id", sa.Integer(), sa.ForeignKey("stock_paper_mandates.id"), nullable=False),
        sa.Column("revoked_by", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        _created_at(),
    )
    _create_index_if_missing(
        bind, "uq_stock_paper_mandate_revocations_mandate", "stock_paper_mandate_revocations",
        ["mandate_id"], unique=True,
    )

    _create_table_if_missing(
        bind, "stock_paper_mandate_approvals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("mandate_id", sa.Integer(), sa.ForeignKey("stock_paper_mandates.id"), nullable=False),
        sa.Column("approval_id", sa.Integer(), sa.ForeignKey("stock_paper_run_approvals.id"), nullable=False),
        sa.Column("cycle_id", sa.String(length=64), sa.ForeignKey("stock_learning_cycles.cycle_id"), nullable=False),
        sa.Column("duration_sessions", sa.Integer(), nullable=False),
        sa.Column("fit", sa.JSON(), nullable=False),
        _created_at(),
    )
    _create_index_if_missing(
        bind, "uq_stock_paper_mandate_approvals_approval", "stock_paper_mandate_approvals",
        ["approval_id"], unique=True,
    )
    _create_index_if_missing(
        bind, "ix_stock_paper_mandate_approvals_mandate_id", "stock_paper_mandate_approvals", ["mandate_id"],
    )
    _create_index_if_missing(
        bind, "ix_stock_paper_mandate_approvals_cycle_id", "stock_paper_mandate_approvals", ["cycle_id"],
    )

    _create_table_if_missing(
        bind, "stock_strategy_readmissions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("strategy_id", sa.Integer(), sa.ForeignKey("strategies.id"), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("current_cycle_id", sa.String(length=64)),
        sa.Column("successor_cycle_id", sa.String(length=64)),
        sa.Column("last_reason", sa.Text()),
        sa.Column("history", sa.JSON(), nullable=False),
        _created_at(),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    _create_index_if_missing(
        bind, "uq_stock_strategy_readmissions_strategy", "stock_strategy_readmissions", ["strategy_id"], unique=True,
    )
    _create_index_if_missing(bind, "ix_stock_strategy_readmissions_status", "stock_strategy_readmissions", ["status"])
    _create_index_if_missing(
        bind, "ix_stock_strategy_readmissions_next_attempt_at", "stock_strategy_readmissions", ["next_attempt_at"],
    )

    _create_table_if_missing(
        bind, "notification_deliveries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("notification_id", sa.Integer(), sa.ForeignKey("notifications.id")),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="alert"),
        sa.Column("dedupe_key", sa.String(length=255), nullable=False),
        sa.Column("severity", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("message", sa.Text()),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("suppressed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        _created_at(),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    _create_index_if_missing(
        bind, "ix_notification_deliveries_channel_key", "notification_deliveries", ["channel", "dedupe_key"],
    )
    _create_index_if_missing(
        bind, "ix_notification_deliveries_notification_id", "notification_deliveries", ["notification_id"],
    )
    _create_index_if_missing(bind, "ix_notification_deliveries_status", "notification_deliveries", ["status"])
    _create_index_if_missing(
        bind, "ix_notification_deliveries_next_attempt_at", "notification_deliveries", ["next_attempt_at"],
    )
    _create_index_if_missing(bind, "ix_notification_deliveries_created_at", "notification_deliveries", ["created_at"])

    if bind.dialect.name == "postgresql":
        op.execute(sa.text(
            """
            CREATE OR REPLACE FUNCTION reject_stock_paper_mandate_mutation()
            RETURNS trigger AS $$
            BEGIN
              RAISE EXCEPTION 'stock paper mandate records are immutable';
            END;
            $$ LANGUAGE plpgsql
            """
        ))
        for table in IMMUTABLE_TABLES:
            name = f"{table}_immutable"
            if not _trigger_exists(bind, name):
                op.execute(sa.text(
                    f"CREATE TRIGGER {name} BEFORE UPDATE OR DELETE ON {table} "
                    "FOR EACH ROW EXECUTE FUNCTION reject_stock_paper_mandate_mutation()"
                ))
    elif bind.dialect.name == "sqlite":
        for table in IMMUTABLE_TABLES:
            for operation in ("update", "delete"):
                name = f"{table}_immutable_{operation}"
                if not _trigger_exists(bind, name):
                    op.execute(sa.text(
                        f"CREATE TRIGGER {name} BEFORE {operation.upper()} ON {table} "
                        "BEGIN SELECT RAISE(ABORT, 'stock paper mandate records are immutable'); END"
                    ))


def downgrade() -> None:
    raise RuntimeError("0064 is forward-only; restore a verified backup to roll back")
