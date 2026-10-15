"""Explicit, non-qualifying Alpaca cash/activity reconciliation contract."""
from alembic import op
import sqlalchemy as sa

revision = "0049_alpaca_activity_ledger"
down_revision = "0048_online_research"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    account_columns = {column["name"] for column in inspector.get_columns("stock_paper_accounts")}
    if bind.dialect.name == "postgresql":
        op.execute(sa.text(
            "ALTER TABLE stock_paper_accounts "
            "ADD COLUMN IF NOT EXISTS activity_contract VARCHAR(32) "
            "DEFAULT 'legacy-v1' NOT NULL"
        ))
        op.execute(sa.text(
            "ALTER TABLE stock_paper_accounts "
            "ADD COLUMN IF NOT EXISTS activity_baseline JSONB"
        ))
    else:
        if "activity_contract" not in account_columns:
            op.add_column(
                "stock_paper_accounts",
                sa.Column("activity_contract", sa.String(32), nullable=False, server_default="legacy-v1"),
            )
        if "activity_baseline" not in account_columns:
            op.add_column(
                "stock_paper_accounts",
                sa.Column("activity_baseline", sa.JSON(), nullable=True),
            )

    activity_columns = {column["name"] for column in inspector.get_columns("stock_paper_broker_activities")}
    with op.batch_alter_table("stock_paper_broker_activities") as batch:
        batch.alter_column("occurred_at", existing_type=sa.DateTime(timezone=True), nullable=True)
        if "normalized_payload" not in activity_columns:
            batch.add_column(sa.Column("normalized_payload", sa.JSON(), nullable=True))


def downgrade():
    raise RuntimeError("0049 is forward-only; restore a verified backup to roll back")
