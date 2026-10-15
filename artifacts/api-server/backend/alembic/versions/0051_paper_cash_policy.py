"""Explicit opt-in paper cash comparison policy; preserve existing exact checks."""
from alembic import op
import sqlalchemy as sa

revision = "0051_paper_cash_policy"
down_revision = "0050_paper_quantity_precision"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.exec_driver_sql(
            "ALTER TABLE stock_paper_accounts "
            "ADD COLUMN IF NOT EXISTS cash_policy VARCHAR(48) "
            "DEFAULT 'exact-v1' NOT NULL"
        )
        return

    inspector = sa.inspect(bind)
    existing = {column["name"] for column in inspector.get_columns("stock_paper_accounts")}
    if "cash_policy" not in existing:
        op.add_column(
            "stock_paper_accounts",
            sa.Column("cash_policy", sa.String(48), nullable=False, server_default="exact-v1"),
        )


def downgrade():
    raise RuntimeError("0051 is forward-only; restore a verified backup to roll back")
