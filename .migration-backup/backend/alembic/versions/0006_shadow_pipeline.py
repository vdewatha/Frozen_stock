"""Version-bound research shadow observation ledger."""
from alembic import op
import sqlalchemy as sa

revision = "0006_shadow_pipeline"
down_revision = "0005_crypto_observations"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("shadow_model_bindings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.String(64), sa.ForeignKey("research_model_runs.run_id"), nullable=False),
        sa.Column("spec_sha256", sa.String(64), unique=True, nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("instrument", sa.String(32), nullable=False),
        sa.Column("timeframe_minutes", sa.Integer(), nullable=False),
        sa.Column("spec", sa.JSON(), nullable=False),
        sa.Column("actor", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()))
    op.create_table("shadow_decisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("binding_id", sa.Integer(), sa.ForeignKey("shadow_model_bindings.id"), nullable=False),
        sa.Column("bar_close", sa.DateTime(), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("data_sha256", sa.String(64), nullable=False),
        sa.Column("spec_sha256", sa.String(64), nullable=False),
        sa.Column("probability", sa.Float(), nullable=False),
        sa.Column("reference_price", sa.Float(), nullable=False),
        sa.Column("intended_action", sa.String(24), nullable=False),
        sa.Column("backfilled", sa.Boolean(), nullable=False),
        sa.Column("eligible_for_qualification", sa.Boolean(), nullable=False),
        sa.Column("latency_seconds", sa.Float(), nullable=False),
        sa.Column("outcome", sa.JSON()),
        sa.UniqueConstraint("binding_id", "bar_close", name="uq_shadow_binding_bar"),
        sa.CheckConstraint("eligible_for_qualification = false", name="ck_shadow_research_only"))
    op.create_table("shadow_run_audits",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("binding_id", sa.Integer(), sa.ForeignKey("shadow_model_bindings.id"), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("reason", sa.String(120), nullable=False))


def downgrade():
    raise RuntimeError("0006 is forward-only; restore a verified backup to roll back")
