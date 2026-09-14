"""Allow immutable blocked promotion decisions to be retried with new evidence."""
from alembic import op
import sqlalchemy as sa


revision = "0033_promotion_decision_retries"
down_revision = "0032_automatic_paper_promotion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    has_legacy_constraint = any(
        constraint.get("name") == "uq_stock_promotion_decision_cycle"
        for constraint in sa.inspect(bind).get_unique_constraints(
            "stock_paper_promotion_decisions"
        )
    )
    if not has_legacy_constraint:
        return
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(
            "stock_paper_promotion_decisions",
            recreate="always",
        ) as batch:
            batch.drop_constraint("uq_stock_promotion_decision_cycle", type_="unique")
    else:
        op.drop_constraint(
            "uq_stock_promotion_decision_cycle",
            "stock_paper_promotion_decisions",
            type_="unique",
        )


def downgrade() -> None:
    raise RuntimeError("0033 is forward-only; restore a verified backup to roll back")