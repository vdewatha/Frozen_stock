"""Bind controlled forward trials to their dedicated strategy."""
from alembic import op
import sqlalchemy as sa

revision = "0017_forward_trial_strategy"
down_revision = "0016_stock_forward_trial"
branch_labels = None
depends_on = None

def upgrade() -> None:
    with op.batch_alter_table("stock_paper_trials") as batch:
        batch.add_column(sa.Column("strategy_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_stock_trial_strategy", "strategies", ["strategy_id"], ["id"])
        batch.create_index("ix_stock_paper_trials_strategy_id", ["strategy_id"])

def downgrade() -> None:
    with op.batch_alter_table("stock_paper_trials") as batch:
        batch.drop_index("ix_stock_paper_trials_strategy_id")
        batch.drop_constraint("fk_stock_trial_strategy", type_="foreignkey")
        batch.drop_column("strategy_id")