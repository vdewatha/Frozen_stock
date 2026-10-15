"""Separate paper-research qualification from legacy complete-fee admission."""
from alembic import op
import sqlalchemy as sa

revision = "0052_paper_research_qual"
down_revision = "0051_paper_cash_policy"
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
    _create_table_if_missing(bind, "stock_paper_research_qualifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("stock_paper_accounts.id"), nullable=False),
        sa.Column("report", sa.JSON(), nullable=False),
        sa.Column("report_sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("reviewed_by", sa.String(128), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False))
    _create_index_if_missing(bind, "ix_stock_paper_research_qualifications_account_id", "stock_paper_research_qualifications", ["account_id"])
    _create_table_if_missing(bind, "stock_paper_research_authorizations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("qualification_id", sa.Integer(), sa.ForeignKey("stock_paper_research_qualifications.id"), nullable=False),
        sa.Column("authorized_by", sa.String(128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("authorization_sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("paper_only", sa.Boolean(), nullable=False),
        sa.Column("live_authorized", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("paper_only = true", name="ck_research_authorization_paper_only"),
        sa.CheckConstraint("live_authorized = false", name="ck_research_authorization_live_disabled"))
    _create_index_if_missing(bind, "ix_stock_paper_research_authorizations_qualification_id", "stock_paper_research_authorizations", ["qualification_id"])


def downgrade():
    raise RuntimeError("0052 is forward-only; restore a verified backup to roll back")
