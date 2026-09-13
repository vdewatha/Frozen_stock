"""stock paper signal-bound entry fields"""
from alembic import op
import sqlalchemy as sa

revision = "0012_stock_paper_signal_binding"
down_revision = "0011_stock_paper_verification"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    evidence_columns = [
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("strategy_id", sa.Integer(), nullable=False),
        sa.Column("evidence_id", sa.String(96), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("verified_drawdown", sa.Numeric(12, 8), nullable=False),
        sa.Column("consecutive_losses", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.UniqueConstraint("strategy_id", name="uq_stock_paper_strategy_evidence_strategy"),
        sa.UniqueConstraint("evidence_id"),
    ]
    # A populated interim stock-training schema may be repaired in an isolated
    # namespace that does not include the application's core strategy tables.
    # Preserve the stock rows and add the relationship constraints when those
    # referenced tables are actually present.
    if "strategies" in tables:
        evidence_columns.append(sa.ForeignKeyConstraint(["strategy_id"], ["strategies.id"]))
    op.create_table("stock_paper_strategy_evidence",
        *evidence_columns)
    with op.batch_alter_table("stock_paper_orders") as batch:
        batch.add_column(sa.Column("strategy_id", sa.Integer()))
        batch.add_column(sa.Column("signal_id", sa.Integer()))
        batch.add_column(sa.Column("evidence_id", sa.String(96)))
        if "strategies" in tables:
            batch.create_foreign_key("fk_stock_paper_orders_strategy", "strategies", ["strategy_id"], ["id"])
        if "strategy_signals" in tables:
            batch.create_foreign_key("fk_stock_paper_orders_signal", "strategy_signals", ["signal_id"], ["id"])
        batch.create_index("ix_stock_paper_orders_strategy_id", ["strategy_id"])
        batch.create_index("ix_stock_paper_orders_signal_id", ["signal_id"], unique=True)


def downgrade():
    with op.batch_alter_table("stock_paper_orders") as batch:
        batch.drop_index("ix_stock_paper_orders_signal_id")
        batch.drop_index("ix_stock_paper_orders_strategy_id")
        batch.drop_constraint("fk_stock_paper_orders_signal", type_="foreignkey")
        batch.drop_constraint("fk_stock_paper_orders_strategy", type_="foreignkey")
        batch.drop_column("evidence_id")
        batch.drop_column("signal_id")
        batch.drop_column("strategy_id")
    op.drop_table("stock_paper_strategy_evidence")