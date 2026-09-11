"""retain unexplained stock-paper accounting residuals"""
from alembic import op
import sqlalchemy as sa


revision = "0014_stock_paper_residual"
down_revision = "0013_stock_paper_event_actor"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("stock_paper_accounts",
                  sa.Column("unexplained_residual", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    op.drop_column("stock_paper_accounts", "unexplained_residual")