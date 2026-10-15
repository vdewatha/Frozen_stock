"""Add an isolated paper-only intraday scalp research lane."""
from alembic import op
import sqlalchemy as sa

revision = "0059_scalp_research"
down_revision = "0058_market_price_provider_key"
branch_labels = None
depends_on = None


def _create_table_if_missing(bind, table_name: str, *elements) -> None:
    if not sa.inspect(bind).has_table(table_name):
        metadata = sa.MetaData()
        for element in elements:
            foreign_keys = getattr(element, "foreign_keys", ())
            for foreign_key in foreign_keys:
                target_table, target_column = foreign_key.target_fullname.rsplit(".", 1)
                if target_table not in metadata.tables:
                    sa.Table(target_table, metadata, sa.Column(target_column, sa.Integer()))
        table = sa.Table(table_name, metadata, *elements)
        bind.execute(sa.schema.CreateTable(table, if_not_exists=True))


def _create_index_if_missing(bind, name: str, table_name: str, columns: list[str]) -> None:
    if name not in {index["name"] for index in sa.inspect(bind).get_indexes(table_name)}:
        op.create_index(name, table_name, columns)


def upgrade():
    bind = op.get_bind()
    _create_table_if_missing(
        bind,
        "scalp_research_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("requested_by", sa.String(length=128), nullable=False),
        sa.Column("symbols", sa.JSON(), nullable=False),
        sa.Column("timeframe", sa.String(length=8), nullable=False, server_default="1m"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("eligible_for_trading", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("data_snapshot", sa.JSON(), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("assumptions", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("run_id", name="uq_scalp_research_run_id"),
        sa.CheckConstraint("status IN ('queued', 'running', 'completed', 'failed', 'blocked')", name="ck_scalp_research_run_status"),
        sa.CheckConstraint("eligible_for_trading = false", name="ck_scalp_research_run_ineligible"),
    )
    _create_index_if_missing(bind, "ix_scalp_research_runs_status", "scalp_research_runs", ["status"])
    _create_index_if_missing(bind, "ix_scalp_research_runs_created_at", "scalp_research_runs", ["created_at"])


def downgrade():
    raise RuntimeError("0054 is forward-only; restore a verified backup to roll back")
