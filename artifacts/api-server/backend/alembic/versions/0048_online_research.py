"""Durable forward-only research predictions; never execution authority."""
from alembic import op
import sqlalchemy as sa

revision = "0048_online_research"
down_revision = "0047_paper_venue_qualification"
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
        "online_research_forecasts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("source_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("target_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("prediction", sa.JSON(), nullable=False),
        sa.Column("outcome", sa.JSON(), nullable=True),
        sa.UniqueConstraint("symbol", "version", "source_at", name="uq_online_research_source"),
    )
    _create_index_if_missing(bind, "ix_online_research_forecasts_symbol", "online_research_forecasts", ["symbol"])
    _create_index_if_missing(bind, "ix_online_research_forecasts_status", "online_research_forecasts", ["status"])


def downgrade():
    raise RuntimeError("0048 is forward-only; restore a verified backup to roll back")
