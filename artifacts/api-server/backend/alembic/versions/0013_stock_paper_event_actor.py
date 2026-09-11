"""stock paper event actor attribution"""
from alembic import op
import sqlalchemy as sa

revision = "0013_stock_paper_event_actor"
down_revision = "0012_stock_paper_signal_binding"
branch_labels = None
depends_on = None

def upgrade():
    with op.batch_alter_table("stock_paper_ledger_events") as batch:
        batch.add_column(sa.Column("actor", sa.String(128), nullable=False, server_default="system"))

def downgrade():
    with op.batch_alter_table("stock_paper_ledger_events") as batch:
        batch.drop_column("actor")