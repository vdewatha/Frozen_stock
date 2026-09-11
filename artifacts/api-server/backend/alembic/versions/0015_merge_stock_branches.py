"""Merge stock training and paper-ledger migration branches."""

revision = "0015_merge_stock_branches"
down_revision = ("0014_stock_holdout_consumption", "0014_stock_paper_residual")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass