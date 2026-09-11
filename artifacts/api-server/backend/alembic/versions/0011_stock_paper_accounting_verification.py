"""stock paper accounting verification state"""
from alembic import op
import sqlalchemy as sa

revision = "0011_stock_paper_verification"
down_revision = "0010_stock_paper_ledger"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("stock_paper_accounts", sa.Column("accounting_verified", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    op.drop_column("stock_paper_accounts", "accounting_verified")