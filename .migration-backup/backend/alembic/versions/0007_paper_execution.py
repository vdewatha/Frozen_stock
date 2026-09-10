"""Paper-only serialized execution ledger."""
from alembic import op
import sqlalchemy as sa

revision = "0007_paper_execution"
down_revision = "0006_shadow_pipeline"
branch_labels = None
depends_on = None


def amount(name):
    # Integer units at 1e-8, matching ExactAmount without SQLite REAL rounding.
    return sa.Column(name, sa.BigInteger(), nullable=False)


def upgrade():
    op.create_table("paper_execution_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        amount("cash"), amount("starting_cash"), amount("reserved_cash"),
        amount("quantity"), amount("reserved_quantity"),
        sa.Column("kill_switch", sa.Boolean(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_paper_account_singleton"))
    op.create_table("paper_trial_approvals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("binding_id", sa.Integer(), sa.ForeignKey("shadow_model_bindings.id"), nullable=False),
        sa.Column("model_run_id", sa.String(64), nullable=False),
        sa.Column("binding_hash", sa.String(64), nullable=False),
        sa.Column("policy_hash", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(128), nullable=False),
        amount("max_notional"), amount("max_exposure"), amount("fee_rate"),
        sa.Column("nonqualifying", sa.Boolean(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()))
    op.create_table("paper_order_intents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("client_order_id", sa.String(128), nullable=False, unique=True),
        sa.Column("decision_id", sa.Integer(), sa.ForeignKey("shadow_decisions.id"), nullable=False),
        sa.Column("approval_id", sa.Integer(), sa.ForeignKey("paper_trial_approvals.id"), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        amount("quantity"), amount("limit_price"), amount("reserved_cash"), amount("reserved_quantity"), amount("filled_quantity"),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("provider_order_id", sa.String(128), nullable=True, unique=True),
        sa.Column("provider_context", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("decision_id", "side", name="uq_paper_decision_side"))
    op.create_table("paper_execution_fills",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider_fill_id", sa.String(128), nullable=False, unique=True),
        sa.Column("intent_id", sa.Integer(), sa.ForeignKey("paper_order_intents.id"), nullable=False),
        amount("quantity"), amount("price"), amount("fee"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()))


def downgrade():
    raise RuntimeError("0007 is forward-only; restore a verified backup to roll back")
