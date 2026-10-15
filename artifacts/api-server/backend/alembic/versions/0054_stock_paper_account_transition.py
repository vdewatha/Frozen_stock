"""Preserve archived stock-paper ledgers across broker-account transitions."""
from alembic import op
import sqlalchemy as sa


revision = "0054_paper_account_transition"
down_revision = "0053_model_prediction_identity"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    account_columns = {column["name"] for column in inspector.get_columns("stock_paper_accounts")}
    constraint_names = {
        item.get("name")
        for item in inspector.get_unique_constraints("stock_paper_accounts")
    }
    index_names = {
        item.get("name")
        for item in inspector.get_indexes("stock_paper_accounts")
    }
    legacy_identity = "uq_stock_paper_account_broker"

    # Drop a publish-preapplied unique index outside batch mode. SQLite's
    # batch table rebuild can otherwise copy the index back into the rebuilt
    # table even when drop_index is requested inside the batch.
    legacy_index_present = legacy_identity in index_names
    if legacy_index_present:
        op.drop_index(legacy_identity, table_name="stock_paper_accounts")

    with op.batch_alter_table("stock_paper_accounts", recreate="auto") as batch_op:
        if legacy_identity in constraint_names and not legacy_index_present:
            batch_op.drop_constraint(legacy_identity, type_="unique")
        if "archived_at" not in account_columns:
            batch_op.add_column(sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
        if "archive_reason" not in account_columns:
            batch_op.add_column(sa.Column("archive_reason", sa.Text(), nullable=True))
        if "ix_stock_paper_accounts_archived_at" not in index_names:
            batch_op.create_index("ix_stock_paper_accounts_archived_at", ["archived_at"])


def downgrade():
    raise RuntimeError("0054 is forward-only; restore a verified backup to roll back")
