"""Add explicit accounting-review evidence to stock paper recovery."""
from alembic import op
import sqlalchemy as sa


revision = "0030_stock_accounting_review"
down_revision = "0029_learning_cycle_compat"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("stock_paper_recovery_state")}
    additions = (
        ("accounting_review_required", sa.Boolean(), False),
        ("accounting_reviewed_at", sa.DateTime(timezone=True), None),
        ("accounting_reviewed_by", sa.String(length=128), None),
        ("accounting_review_reason", sa.Text(), None),
        ("accounting_review_digest", sa.String(length=64), None),
    )
    for name, column_type, default in additions:
        if name in columns:
            continue
        kwargs = {"nullable": False, "server_default": sa.false()} if default is False else {}
        op.add_column(
            "stock_paper_recovery_state",
            sa.Column(name, column_type, **kwargs),
        )
    op.execute(
        sa.text(
            "UPDATE stock_paper_recovery_state "
            "SET accounting_review_required = TRUE "
            "WHERE EXISTS ("
            "SELECT 1 FROM stock_paper_accounts "
            "WHERE stock_paper_accounts.unexplained_residual = TRUE"
            ")"
        )
    )


def downgrade() -> None:
    raise RuntimeError("0030 is forward-only; restore a verified backup to roll back")
