"""Paper-only loss feedback loop: trade attribution, governed parameter sets,
per-strategy meta-label modes, and performance allocation weights.

Attributions, parameter sets, meta-label mode events, and allocation weights
are append-only (immutable via triggers). Parameter applications and the
current meta-label mode are mutable operational state.
"""
from alembic import op
import sqlalchemy as sa


revision = "0065_loss_feedback_loop"
down_revision = "0064_unattended_paper"
branch_labels = None
depends_on = None

IMMUTABLE_TABLES = (
    "stock_trade_attributions",
    "stock_parameter_sets",
    "stock_meta_label_mode_events",
    "stock_allocation_weights",
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


def _updated_at():
    return sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def upgrade() -> None:
    bind = op.get_bind()
    _create_table_if_missing(
        bind, "stock_trade_attributions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_key", sa.String(length=128), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("attribution_sha256", sa.String(length=64), nullable=False),
        sa.Column("strategy_id", sa.Integer(), nullable=True),
        sa.Column("strategy_type", sa.String(length=64), nullable=False),
        sa.Column("trial_id", sa.String(length=36), nullable=True),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exit_cause", sa.String(length=32), nullable=False),
        sa.Column("net_return", sa.Numeric(18, 10), nullable=True),
        sa.Column("gross_return", sa.Numeric(18, 10), nullable=False),
        sa.Column("net_pnl", sa.Numeric(20, 8), nullable=True),
        sa.Column("costs_known", sa.Boolean(), nullable=False),
        sa.Column("is_loss", sa.Boolean(), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("paper_only", sa.Boolean(), nullable=False),
        _created_at(),
    )
    _create_index_if_missing(bind, "uq_stock_trade_attributions_source_revision", "stock_trade_attributions",
                             ["source_key", "revision"], unique=True)
    _create_index_if_missing(bind, "ix_stock_trade_attributions_strategy_type", "stock_trade_attributions", ["strategy_type"])
    _create_index_if_missing(bind, "ix_stock_trade_attributions_symbol", "stock_trade_attributions", ["symbol"])
    _create_index_if_missing(bind, "ix_stock_trade_attributions_closed_at", "stock_trade_attributions", ["closed_at"])
    _create_index_if_missing(bind, "ix_stock_trade_attributions_trial_id", "stock_trade_attributions", ["trial_id"])

    _create_table_if_missing(
        bind, "stock_parameter_sets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("strategy_type", sa.String(length=64), nullable=False),
        sa.Column("strategy_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("parameters_sha256", sa.String(length=64), nullable=False),
        sa.Column("base_parameters", sa.JSON(), nullable=False),
        sa.Column("experiment_id", sa.Integer(), nullable=True),
        sa.Column("lineage", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("paper_only", sa.Boolean(), nullable=False),
        _created_at(),
    )
    _create_index_if_missing(bind, "uq_stock_parameter_sets_type_version", "stock_parameter_sets",
                             ["strategy_type", "version"], unique=True)
    _create_index_if_missing(bind, "uq_stock_parameter_sets_type_sha256", "stock_parameter_sets",
                             ["strategy_type", "parameters_sha256"], unique=True)

    _create_table_if_missing(
        bind, "stock_parameter_applications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("parameter_set_id", sa.Integer(), nullable=False),
        sa.Column("strategy_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("cycle_id", sa.String(length=64), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_reason", sa.Text(), nullable=True),
        sa.Column("history", sa.JSON(), nullable=False),
        _created_at(),
        _updated_at(),
    )
    _create_index_if_missing(bind, "uq_stock_parameter_applications_set", "stock_parameter_applications",
                             ["parameter_set_id"], unique=True)
    _create_index_if_missing(bind, "ix_stock_parameter_applications_type_status", "stock_parameter_applications",
                             ["strategy_type", "status"])

    _create_table_if_missing(
        bind, "stock_meta_label_modes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("strategy_type", sa.String(length=64), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        _updated_at(),
    )
    _create_index_if_missing(bind, "uq_stock_meta_label_modes_type", "stock_meta_label_modes",
                             ["strategy_type"], unique=True)

    _create_table_if_missing(
        bind, "stock_meta_label_mode_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("strategy_type", sa.String(length=64), nullable=False),
        sa.Column("from_mode", sa.String(length=16), nullable=False),
        sa.Column("to_mode", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        _created_at(),
    )
    _create_index_if_missing(bind, "ix_stock_meta_label_mode_events_type", "stock_meta_label_mode_events",
                             ["strategy_type"])

    _create_table_if_missing(
        bind, "stock_allocation_weights",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("strategy_type", sa.String(length=64), nullable=False),
        sa.Column("multiplier", sa.Numeric(10, 6), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("paper_only", sa.Boolean(), nullable=False),
        _created_at(),
    )
    _create_index_if_missing(bind, "uq_stock_allocation_weights_session_type", "stock_allocation_weights",
                             ["session_date", "strategy_type"], unique=True)

    if bind.dialect.name == "postgresql":
        op.execute(sa.text(
            """
            CREATE OR REPLACE FUNCTION reject_loss_feedback_mutation()
            RETURNS trigger AS $$
            BEGIN
              RAISE EXCEPTION 'loss feedback evidence records are immutable';
            END;
            $$ LANGUAGE plpgsql
            """
        ))
        for table in IMMUTABLE_TABLES:
            name = f"{table}_immutable"
            if not _trigger_exists(bind, name):
                op.execute(sa.text(
                    f"CREATE TRIGGER {name} BEFORE UPDATE OR DELETE ON {table} "
                    "FOR EACH ROW EXECUTE FUNCTION reject_loss_feedback_mutation()"
                ))
    elif bind.dialect.name == "sqlite":
        for table in IMMUTABLE_TABLES:
            for operation in ("update", "delete"):
                name = f"{table}_immutable_{operation}"
                if not _trigger_exists(bind, name):
                    op.execute(sa.text(
                        f"CREATE TRIGGER {name} BEFORE {operation.upper()} ON {table} "
                        "BEGIN SELECT RAISE(ABORT, 'loss feedback evidence records are immutable'); END"
                    ))


def downgrade() -> None:
    raise RuntimeError("0065 is forward-only; restore a verified backup to roll back")
