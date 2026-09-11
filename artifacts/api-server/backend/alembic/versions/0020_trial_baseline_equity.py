"""Durable immutable trial equity baseline."""
from alembic import op
import sqlalchemy as sa

revision = "0020_trial_baseline_equity"
down_revision = "0019_trial_lot_exit_intent"
branch_labels = None
depends_on = None

def upgrade() -> None:
    with op.batch_alter_table("stock_paper_trials") as batch:
        batch.add_column(sa.Column("baseline_equity", sa.Numeric(20, 8)))
        batch.add_column(sa.Column("baseline_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("peak_equity", sa.Numeric(20, 8)))

def downgrade() -> None:
    with op.batch_alter_table("stock_paper_trials") as batch:
        batch.drop_column("peak_equity")
        batch.drop_column("baseline_at")
        batch.drop_column("baseline_equity")