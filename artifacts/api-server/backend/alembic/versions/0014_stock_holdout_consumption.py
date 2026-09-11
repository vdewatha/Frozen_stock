"""Record fenced one-time consumption of reserved stock holdouts."""
from alembic import op
import sqlalchemy as sa


revision = "0014_stock_holdout_consumption"
down_revision = "0013_stock_registry_immutable"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "stock_holdout_consumptions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "reservation_id", sa.Integer(),
            sa.ForeignKey("stock_holdout_reservations.id"), nullable=False,
        ),
        sa.Column(
            "job_id", sa.String(36),
            sa.ForeignKey("stock_training_jobs.id"), nullable=False,
        ),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("claim_sha256", sa.String(64), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("reservation_id", name="uq_stock_holdout_consumption_reservation"),
        sa.UniqueConstraint("claim_sha256", name="uq_stock_holdout_consumption_claim"),
    )
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute(
            "CREATE TRIGGER stock_holdout_consumptions_immutable_update "
            "BEFORE UPDATE ON stock_holdout_consumptions "
            "BEGIN SELECT RAISE(ABORT, 'stock_holdout_consumptions is immutable'); END"
        )
        op.execute(
            "CREATE TRIGGER stock_holdout_consumptions_immutable_delete "
            "BEFORE DELETE ON stock_holdout_consumptions "
            "BEGIN SELECT RAISE(ABORT, 'stock_holdout_consumptions is immutable'); END"
        )
    else:
        op.execute(
            "CREATE OR REPLACE FUNCTION stock_training_immutable_row() RETURNS trigger AS "
            "$$ BEGIN RAISE EXCEPTION 'stock training audit rows are immutable'; END; $$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE TRIGGER stock_holdout_consumptions_immutable "
            "BEFORE UPDATE OR DELETE ON stock_holdout_consumptions "
            "FOR EACH ROW EXECUTE FUNCTION stock_training_immutable_row()"
        )


def downgrade():
    raise RuntimeError("0014 is forward-only; restore a verified backup to roll back")